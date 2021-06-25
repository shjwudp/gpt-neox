# coding=utf-8
#
# Copyright 2021 Biderman et al. This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT-2 model."""

from logging import error
import torch
from collections import defaultdict

from functools import partial
from megatron.model.utils import Lambda, SequentialWrapper
from megatron.model.norms import LayerNorm, RMSNorm, ScaleNorm
from megatron.model.init_functions import get_init_methods

from megatron import mpu, print_rank_0
from megatron.mpu import ParallelRelativePositionBias
import megatron.fp16 as fp16
from megatron.model.transformer import ParallelTransformerLayerPipe, NormPipe, ParallelLinearPipe, parallel_lm_logits
from megatron.model.transformer import ParallelTransformerLayerDistilPipe, NormDistilPipe, ParallelLinearDistilPipe

from megatron.model.word_embeddings import EmbeddingPipe, EmbeddingDistilPipe

# Pipeline parallelism
from deepspeed.pipe import PipelineModule, LayerSpec, TiedLayerSpec


def gpt2_attention_mask_func(attention_scores, ltor_mask):
    attention_scores.masked_fill_(ltor_mask, -10000.0)
    return attention_scores


def cross_entropy(output, labels, _fp16=False):
    """ From pretrain_gpt2:forward_step() """
    """
    if self.fp16_lm_cross_entropy:
        assert output.dtype == torch.half
        loss = mpu.vocab_parallel_cross_entropy(output, labels)
    else:
        loss = mpu.vocab_parallel_cross_entropy(output.float(), labels)
        return loss
    """
    labels, loss_mask = labels[0], labels[1]
    if _fp16:
        assert (output.dtype == torch.half and loss_mask.dtype == torch.half)
        losses = mpu.vocab_parallel_cross_entropy(output.contiguous(), labels)
    else:
        output = fp16.fp16_to_fp32(output)
        losses = mpu.vocab_parallel_cross_entropy(output.contiguous(), labels)
    loss_mask = loss_mask.view(-1)
    loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()
    return loss

def kldiv_loss(output, labels, _fp16=False):

    labels, loss_mask = labels[0], labels[1]
    if _fp16:
        assert (output.dtype == torch.half and labels.dtype == torch.half and loss_mask.dtype == torch.half)
        losses = mpu.loss.vocab_parallel_KLDivLoss(output.contiguous(), labels.contiguous())
    else:
        output = fp16.fp16_to_fp32(output)
        labels = fp16.fp16_to_fp32(labels)
        losses = mpu.loss.vocab_parallel_KLDivLoss(output.contiguous(), labels.contiguous())
    loss_mask = loss_mask.view(-1)
    loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()
    return loss

def mse_loss(output, labels, _fp16=False):

    labels, loss_mask = labels[0], labels[1]
    if _fp16:
        assert (output.dtype == torch.half and labels.dtype == torch.half and loss_mask.dtype == torch.half)
        losses = mpu.loss.vocab_parallel_KLDivLoss(output.contiguous(), labels.contiguous())
    else:
        output = fp16.fp16_to_fp32(output)
        losses = mpu.loss.vocab_parallel_KLDivLoss(output.contiguous(), labels.contiguous())
        labels = fp16.fp16_to_fp32(labels)
    loss_mask = loss_mask.view(-1)
    loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()
    return loss

def combined_loss(output, labels, alpha_lm=0, alpha_kld=0, alpha_mse=0, _fp16=False):

    labels, loss_mask = labels[0], labels[1]
    teacher_logits, teacher_outputs, student_logits, student_output = output
    if alpha_lm > 0:
        lm_loss = cross_entropy(student_logits, (labels, loss_mask), _fp16=_fp16)
        loss = alpha_lm * lm_loss

    if alpha_kld > 0:
        kl_loss = kldiv_loss(student_logits, (teacher_logits, loss_mask),  _fp16=_fp16)
        loss += alpha_kld * kl_loss

    if alpha_mse > 0:
        ms_loss = mse_loss(student_logits, (teacher_logits, loss_mask), _fp16=_fp16)
        loss += alpha_mse * ms_loss

    return loss

def substitue_args(neox_args, set_student_args=True):
    if neox_args.do_distillation:
        args_to_substitue = neox_args.student_model_args \
            if set_student_args else neox_args.teacher_model_args
        for arg in args_to_substitue.__dict__:
            if args_to_substitue.__dict__[arg] is not None:
                neox_args.__dict__[arg] = args_to_substitue.__dict__[arg]
    return neox_args

class GPT2ModelPipe(PipelineModule, torch.nn.Module):
    """GPT2Model adapted for pipeline parallelism.

    The largest change is flattening the GPTModel class so we can express it as a
    sequence of layers including embedding, transformer layers, and output.
    """

    def __init__(self, neox_args, num_tokentypes=0, parallel_output=True, topology=None, inference=False, get_key_value=True):
        self.neox_args = neox_args

        self._inference = inference
        self.get_key_value = get_key_value if inference else False
        self.parallel_output = parallel_output
        self.do_distillation = self.neox_args.do_distillation
        self.specs = []

        if self.do_distillation:
            if self._inference == True:
                raise AssertionError("Cannot use distill model for inference !")
            list_neox_args = [substitue_args(neox_args, set_student_args=False), 
                              substitue_args(neox_args, set_student_args=True)]
        else:
            list_neox_args = [neox_args] 

        for neox_args in list_neox_args:
            self.neox_args = neox_args
            self.hidden_size = self.neox_args.hidden_size
            self.num_tokentypes = num_tokentypes
            self.init_method, self.output_layer_init_method = get_init_methods(self.neox_args)
            self.fp16_lm_cross_entropy = self.neox_args.fp16_lm_cross_entropy
            self.embedding_type = self.neox_args.pos_emb
            self.init_specs()

        if self.do_distillation:
            loss_fn = partial(combined_loss, 
                            alpha_lm=self.neox_args.alpha_lm, 
                            alpha_kld=self.neox_args.alpha_kld, 
                            alpha_mse=self.neox_args.alpha_mse, 
                            _fp16=self.fp16_lm_cross_entropy)
        else:
            loss_fn = partial(cross_entropy, _fp16=self.fp16_lm_cross_entropy)

        if self.neox_args.checkpoint_activations:
            interval = self.neox_args.checkpoint_num_layers
        else:
            interval = 0
        super().__init__(layers=self.specs,
                        loss_fn=loss_fn if not self._inference else None,
                        topology=topology,
                        activation_checkpoint_interval=interval,
                        partition_method='type:transformer')

    def init_specs(self):
        weight_tying = not self.neox_args.no_weight_tying
        if self.embedding_type == 'rpe':
            rpe_emb = ParallelRelativePositionBias(neox_args=self.neox_args, causal=True, num_buckets=self.neox_args.rpe_num_buckets,
                                                   max_distance=self.neox_args.rpe_max_distance,
                                                   heads=self.neox_args.num_attention_heads)
        self.fp16_lm_cross_entropy = self.neox_args.fp16_lm_cross_entropy

        #
        # forward() prototype
        # 
        # Embedding layer
        # input will be (input_ids, position_ids, attention_mask) in Training
        # and (input_ids, position_ids, attention_mask, layer_past) in Inference
        student_embed_name = ""
        if weight_tying:
            #if len(self.specs) > 0 its distillation and teacher layers ar there
            student_embed_name = "_s" if len(self.specs) > 0 else ""
            self.specs.append(TiedLayerSpec('embed'+student_embed_name,
                                            EmbeddingDistilPipe if self.do_distillation else EmbeddingPipe,
                                            self.neox_args,
                                            self.hidden_size,
                                            self.neox_args.padded_vocab_size,
                                            self.neox_args.max_position_embeddings,
                                            self.neox_args.hidden_dropout,
                                            self.init_method,
                                            self.num_tokentypes,
                                            tied_weight_attr='word_embeddings_weight'))
        else:
            self.specs.append(LayerSpec(EmbeddingDistilPipe if self.do_distillation else EmbeddingPipe,
                                        self.neox_args,
                                        self.hidden_size,
                                        self.neox_args.padded_vocab_size,
                                        self.neox_args.max_position_embeddings,
                                        self.neox_args.hidden_dropout,
                                        self.init_method,
                                        self.num_tokentypes))

        # NB: in inference, the attention mask always needs to be the *last* item in the args when being passed from 
        # one stage to the next, because deepspeed is hacks on top of hacks.
        #
        # outputs are now
        #           Train: (hidden_states, ((maybe) rotary_pos_emb), attention_mask)
        #           Inference: (hidden_states, layer_past, ((maybe) rotary_pos_emb), attention_mask)
        # 
        # data format change for hidden_states to avoid explicit tranposes : [b s h] --> [s b h]

        if self._inference:
            # we need to add a container to cache `presents` from each layer's forward pass
            # inputs/outputs are now (hidden_states, layer_past, presents, attention_mask)
            self.specs.append(lambda x: (x[0].transpose(0, 1).contiguous(), x[1], torch.Tensor(), *x[2:]))
        else:
            self.specs.append(lambda x: (x[0].transpose(0, 1).contiguous(), *x[1:]))

        # Transformer layers
        for x in range(self.neox_args.num_layers):
            self.specs.append(
                LayerSpec(
                    ParallelTransformerLayerDistilPipe if self.do_distillation else ParallelTransformerLayerPipe,
                    neox_args=self.neox_args,
                    attention_mask_func=gpt2_attention_mask_func,
                    init_method=self.init_method,
                    output_layer_init_method=self.output_layer_init_method,
                    layer_number=x,
                    rpe=rpe_emb if self.neox_args.pos_emb == 'rpe' else None,
                    rotary=self.neox_args.pos_emb == 'rotary',
                    get_key_value=self.get_key_value
                    )
                )

        if self._inference:
            # we can get rid of the mask / pasts / (?rotary_pos_emb) now
            # from (hidden_states, layer_past, presents, (maybe rotary_pos_emb), attention_mask)
            # to (hidden_states^T, presents)
            self.specs.append(lambda x: (x[0].transpose(0, 1).contiguous(), x[2]))
        else:
            if self.do_distillation:
                # Undo data format change
                self.specs.append(lambda x: (x[0].transpose(0, 1).contiguous(), *x[1:]))
            else:
                # Undo data format change and drop mask
                self.specs.append(lambda x: x[0].transpose(0, 1).contiguous())

        # Final layernorm after transformer layers
        if self.neox_args.norm == "rmsnorm":
            norm = RMSNorm
            eps = self.neox_args.rms_norm_epsilon
        elif self.neox_args.norm == "layernorm":
            eps = self.neox_args.layernorm_epsilon
            norm = LayerNorm
        elif self.neox_args.norm == "scalenorm":
            eps = self.neox_args.scalenorm_epsilon
            norm = ScaleNorm

        # NormPipe is a helper class to pass presents through to the output when doing inference
        self.specs.append(
            LayerSpec(NormDistilPipe if self.do_distillation else NormPipe,
                      norm,
                      self.neox_args.hidden_size,
                      eps=eps))

        # outputs are now
        #           Train: hidden_states
        #           Inference: (hidden_states, presents)

        # XXX forward_method_parallel_output is assumed to be None, but we're not in a
        # fwd method to assert

        def _logits_helper(embedding, lm_output):
            """Just a wrapper to massage inputs/outputs from pipeline. """
            if self._inference and len(lm_output) == 2:
                hidden_states, presents = lm_output
                logits = parallel_lm_logits(
                    hidden_states,
                    embedding.word_embeddings_weight,
                    self.parallel_output)
                return logits, presents
            elif self.do_distillation:
                hidden_states = lm_output[0]
                logits = parallel_lm_logits(
                    lm_output,
                    embedding.word_embeddings_weight,
                    self.parallel_output)
                return logits, lm_output[1:]
            else:
                logits = parallel_lm_logits(
                    lm_output,
                    embedding.word_embeddings_weight,
                    self.parallel_output)
                return logits

        if weight_tying:
            self.specs.append(
                TiedLayerSpec('embed'+student_embed_name,
                              EmbeddingDistilPipe if self.do_distillation else EmbeddingPipe,
                              self.neox_args,
                              self.hidden_size,
                              self.neox_args.padded_vocab_size,
                              self.neox_args.max_position_embeddings,
                              self.neox_args.hidden_dropout,
                              self.init_method,
                              self.num_tokentypes,
                              forward_fn=_logits_helper,
                              tied_weight_attr='word_embeddings_weight')
            )
        else:
            self.specs.append(
                LayerSpec(
                    ParallelLinearDistilPipe if self.do_distillation else ParallelLinearPipe,
                    neox_args=self.neox_args,
                    init_method=self.init_method,
                    parallel_output=self.parallel_output
                )
            )
        # so output in training should just be logits
        # in inference it will be (logits, presents) (assuming get_key_value) is true

    def to_sequential(self):
        """
        Transforms the PipelineModule to a plain nn.Sequential module
        :return:
        """
        layers = []
        tied_layers = defaultdict(list)
        for n, spec in enumerate(self.specs):
            if isinstance(spec, TiedLayerSpec):
                if spec.key in tied_layers:
                    # receiver
                    layers.append(Lambda(lambda x: spec.forward_fn(tied_layers[spec.key][0], x)))
                else:
                    # owner
                    module = spec.build(log=False)
                    layers.append(module)
                    tied_layers[spec.key].append(module)
            elif isinstance(spec, LayerSpec):
                layers.append(spec.build(log=False))
            elif hasattr(spec, '__call__'):
                # check that it's a callable function
                layers.append(Lambda(spec))
            else:
                raise ValueError(f'Layer number {n} ({spec}) Not recognized')
        model = SequentialWrapper(layers,
                                  self.activation_checkpoint_interval,
                                  self.activation_checkpoint_func,
                                  parent_class_name=self.__class__.__name__)
        return model
        