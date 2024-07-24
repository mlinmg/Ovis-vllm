import os
from datetime import datetime
from importlib import import_module
from typing import List, Union, Callable, Optional

import deepspeed
import torch
from torch import Tensor, LongTensor, IntTensor
from torch.nn import init
from transformers import PreTrainedModel, AutoConfig, AutoModel, AutoTokenizer, AutoModelForCausalLM
from transformers.generation.utils import GenerateOutput
from transformers.integrations.deepspeed import is_deepspeed_zero3_enabled, deepspeed_config

from ovis.model.configuration_ovis import OvisConfig
from ovis.model.conversation_formatter import ConversationFormatter
from ovis.util.constants import IGNORE_INDEX, IMAGE_TOKEN_INDEX, BEGIN_LINE, END_LINE
from ovis.util.utils import rank0_print


class VisualEmbedding(torch.nn.Embedding):
    def forward(self, input: Tensor) -> Tensor:
        if any((isinstance(input, LongTensor), isinstance(input, IntTensor))):
            return super().forward(input)
        return torch.matmul(input, self.weight)

    def reset_parameters(self, mean=0., std=1.) -> None:
        init.normal_(self.weight, mean=mean, std=std)
        self._fill_padding_idx_with_zero()


class OvisPreTrainedModel(PreTrainedModel):
    config_class = OvisConfig
    base_model_prefix = "ovis"


class Ovis(OvisPreTrainedModel):

    def __init__(self, config: OvisConfig, *inputs, **kwargs):
        super().__init__(config, *inputs, **kwargs)
        if kwargs.get('train_from_scratch'):
            self.llm = kwargs['llm']
            self.generation_config = self.llm.generation_config
            self.config.llm_config = self.llm.config
            self.config.hidden_size = self.llm.config.hidden_size  # for deepspeed auto configuration
            self.text_tokenizer = kwargs['text_tokenizer']
            self.visual_tokenizer = kwargs['visual_tokenizer']
            self.config.visual_tokenizer_config = self.visual_tokenizer.config
        else:
            self.llm = AutoModelForCausalLM.from_config(self.config.llm_config)
            assert self.config.hidden_size == self.llm.config.hidden_size, "hidden size mismatch"
            self.text_tokenizer = AutoTokenizer.from_pretrained(self.config.name_or_path)
            self.visual_tokenizer = AutoModel.from_config(self.config.visual_tokenizer_config,
                                                          image_processor_name_or_path=self.config.name_or_path)

        # initialize vte
        if is_deepspeed_zero3_enabled():
            with deepspeed.zero.Init(config_dict_or_path=deepspeed_config()):
                self.vte = VisualEmbedding(self.config.visual_tokenizer_config.vocab_size, self.config.hidden_size)
        else:
            self.vte = VisualEmbedding(self.config.visual_tokenizer_config.vocab_size, self.config.hidden_size,
                                       device=self.visual_tokenizer.device, dtype=self.visual_tokenizer.dtype)

        def _merge_modules(modules_list: tuple):
            merged_modules = []
            for modules in modules_list:
                merged_modules.extend(modules if modules else [])
            return merged_modules

        self._no_split_modules = _merge_modules((self.llm._no_split_modules, self.visual_tokenizer._no_split_modules))
        self._skip_keys_device_placement = self.llm._skip_keys_device_placement
        self._keep_in_fp32_modules = _merge_modules(
            (self.llm._keep_in_fp32_modules, self.visual_tokenizer._keep_in_fp32_modules))
        self.is_parallelizable = all((self.llm.is_parallelizable, self.visual_tokenizer.is_parallelizable))
        self.supports_gradient_checkpointing = all(
            (self.llm.supports_gradient_checkpointing, self.visual_tokenizer.supports_gradient_checkpointing))
        self._supports_flash_attn_2 = all(
            (self.llm._supports_flash_attn_2, self.visual_tokenizer._supports_flash_attn_2))
        self._supports_sdpa = all((self.llm._supports_sdpa, self.visual_tokenizer._supports_sdpa))

    def get_text_tokenizer(self):
        return self.text_tokenizer

    def get_visual_tokenizer(self):
        return self.visual_tokenizer

    def re_init_vte(self, mean, std):
        vte = self.get_vte()
        rank0_print(BEGIN_LINE)
        rank0_print(f'[{datetime.now()}] Before re-initialization of vte: ')
        with deepspeed.zero.GatheredParameters([vte.weight]):
            rank0_print(f'vte.weight: {vte.weight}')
        with deepspeed.zero.GatheredParameters([vte.weight], modifier_rank=0):
            if not is_deepspeed_zero3_enabled() or deepspeed.comm.get_rank() == 0:
                vte.reset_parameters(mean, std)
        rank0_print(f'[{datetime.now()}] After re-initialization of vte:')
        with deepspeed.zero.GatheredParameters([vte.weight]):
            rank0_print(f'vte.weight: {vte.weight}')
        rank0_print(END_LINE)

    def get_monitor_tensors(self):
        monitor_tensors = dict(
            wte=self.get_wte().weight,
            lm_head=self.get_lm_head().weight,
            vte=self.get_vte().weight
        )
        monitor_tensors.update(
            {f'visual_tokenizer_{k}': v for k, v in self.get_visual_tokenizer().get_monitor_tensors().items()})
        return monitor_tensors

    def get_lm_head(self):
        return self.get_llm().get_output_embeddings()

    def get_llm(self):
        return self.llm

    def get_vte(self):
        return self.vte

    def get_wte(self):
        return self.llm.get_input_embeddings()

    def get_conversation_formatter(self) -> ConversationFormatter:
        if getattr(self, 'conversation_formatter', None) is None:
            self.conversation_formatter = getattr(import_module(".conversation_formatter", __package__),
                                                  self.config.conversation_formatter_class)(self.text_tokenizer)
        return self.conversation_formatter

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        labels: Optional[torch.Tensor],
        pixel_values: List[Optional[torch.Tensor]],
        **kwargs
    ):
        assert self.training, "`forward` can only be used in training. For inference, use `generate`."
        _, inputs_embeds, labels, attention_mask = self.merge_multimodal(
            text_input_ids=input_ids,
            text_attention_masks=attention_mask,
            text_labels=labels,
            pixel_values=pixel_values
        )
        return self.llm(inputs_embeds=inputs_embeds, labels=labels, attention_mask=attention_mask, **kwargs)

    def merge_multimodal(
            self,
            text_input_ids: torch.Tensor,
            text_attention_masks: torch.Tensor,
            text_labels: Optional[torch.Tensor],
            pixel_values: List[Optional[torch.Tensor]]
    ):
        input_device = text_input_ids.device
        if self.training:
            # When training, to be compatible with deepspeed zero, each sample has to include pixel_value tensor.
            # For text-only sample, one can simply use a full zero tensor as pixel_value, which will be ignored
            # (see below in this function); so, the gradient will not be affected.
            num_images = [x.shape[0] for x in pixel_values]
            visual_tokens = self.visual_tokenizer(torch.cat([x for x in pixel_values], dim=0))
            visual_embeds = torch.split(self.get_vte()(visual_tokens).to(dtype=self.dtype, device=input_device),
                                        split_size_or_sections=num_images, dim=0)
            visual_input_ids = torch.split(torch.argmax(visual_tokens, dim=-1).to(device=input_device),
                                           split_size_or_sections=num_images, dim=0)
            visual_labels = [torch.full(x.shape, IGNORE_INDEX, dtype=torch.long, device=input_device) for x in
                             visual_input_ids]
        else:
            # When inference, sample can include only text with `None` pixel_value
            num_images = [x.shape[0] if x is not None else 0 for x in pixel_values]
            if sum(num_images) > 0:
                visual_tokens = self.visual_tokenizer(torch.cat([x for x in pixel_values if x is not None], dim=0))
                visual_embeds = torch.split(self.get_vte()(visual_tokens).to(dtype=self.dtype, device=input_device),
                                            split_size_or_sections=num_images, dim=0)
                visual_input_ids = torch.split(torch.argmax(visual_tokens, dim=-1).to(device=input_device),
                                               split_size_or_sections=num_images, dim=0)
                visual_labels = [torch.full(x.shape, IGNORE_INDEX, dtype=torch.long, device=input_device) for x in
                                 visual_input_ids]
            else:
                # just placeholders
                visual_embeds = [None] * len(num_images)
                visual_input_ids = [None] * len(num_images)
                visual_labels = [None] * len(num_images)
            # just placeholders
            text_labels = torch.full(text_input_ids.shape, IGNORE_INDEX, dtype=torch.long, device=input_device)

        input_embeds = []
        attention_masks = []
        labels = []
        for text_input_id, text_label, text_attention_mask, visual_embed, visual_input_id, visual_label in zip(
                text_input_ids, text_labels, text_attention_masks, visual_embeds, visual_input_ids, visual_labels
        ):
            image_token_mask = torch.eq(text_input_id, IMAGE_TOKEN_INDEX)
            text_embed = self.get_wte()(torch.masked_fill(text_input_id, image_token_mask, 0))
            image_token_positions = torch.where(image_token_mask)[0].tolist()
            if len(image_token_positions) > 0:
                input_embed_parts = []
                attention_mask_parts = []
                label_parts = []
                prev_image_token_position = -1
                for index, image_token_position in enumerate(image_token_positions):
                    input_embed_parts.append(
                        text_embed[prev_image_token_position + 1:image_token_position, :])
                    label_parts.append(
                        text_label[prev_image_token_position + 1:image_token_position])
                    attention_mask_parts.append(
                        text_attention_mask[prev_image_token_position + 1:image_token_position])
                    input_embed_parts.append(visual_embed[index])
                    attention_mask_parts.append(
                        torch.ones_like(visual_label[index], dtype=torch.bool))
                    label_parts.append(visual_label[index])
                    prev_image_token_position = image_token_position
                if prev_image_token_position + 1 < text_input_id.shape[0]:
                    input_embed_parts.append(
                        text_embed[prev_image_token_position + 1:, :])
                    attention_mask_parts.append(
                        text_attention_mask[prev_image_token_position + 1:])
                    label_parts.append(
                        text_label[prev_image_token_position + 1:])
                input_embed = torch.cat(input_embed_parts, dim=0)
                attention_mask = torch.cat(attention_mask_parts, dim=0)
                label = torch.cat(label_parts, dim=0)
            else:
                input_embed = text_embed
                attention_mask = text_attention_mask
                label = text_label
                if self.training:
                    # Make visual_embed involved in the backward graph, to be compatible with deepspeed zero and ddp.
                    input_embed += torch.sum(visual_embed * 0.0)
            input_embeds.append(input_embed)
            attention_masks.append(attention_mask)
            labels.append(label)

        batch_input_embeds = torch.nn.utils.rnn.pad_sequence(input_embeds, batch_first=True, padding_value=0.0)[:,
                             :self.config.multimodal_max_length, :]
        batch_attention_mask = torch.nn.utils.rnn.pad_sequence(attention_masks, batch_first=True, padding_value=False)[
                               :,
                               :self.config.multimodal_max_length]
        batch_labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)[:,
                       :self.config.multimodal_max_length]

        return visual_input_ids, batch_input_embeds, batch_labels, batch_attention_mask

    def save_pretrained(
        self,
        save_directory: Union[str, os.PathLike],
        is_main_process: bool = True,
        state_dict: Optional[dict] = None,
        save_function: Callable = torch.save,
        push_to_hub: bool = False,
        max_shard_size: Union[int, str] = "5GB",
        safe_serialization: bool = True,
        variant: Optional[str] = None,
        token: Optional[Union[str, bool]] = None,
        save_peft_format: bool = True,
        **kwargs,
    ):
        super().save_pretrained(save_directory,
                                is_main_process=is_main_process,
                                state_dict=state_dict,
                                save_function=save_function,
                                safe_serialization=safe_serialization)
        self.get_text_tokenizer().save_pretrained(save_directory)
        self.get_visual_tokenizer().get_image_processor().save_pretrained(save_directory)

        # uncomment the following will additionally save a separate visual tokenizer
        # visual_tokenizer_directory = os.path.join(save_directory, 'visual_tokenizer')
        # self.get_visual_tokenizer().save_pretrained(visual_tokenizer_directory,
        #                                             is_main_process=is_main_process,
        #                                             state_dict=None,
        #                                             save_function=save_function,
        #                                             safe_serialization=safe_serialization)
        # self.get_visual_tokenizer().get_image_processor().save_pretrained(visual_tokenizer_directory)

    # TODO: support batch generation
    def generate(
        self,
        inputs: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Union[GenerateOutput, torch.LongTensor]:
        assert inputs.shape[0] == 1, 'Currently, only support `batch_size=1`'
        _, inputs_embeds, labels, attention_mask = self.merge_multimodal(
            text_input_ids=inputs,
            text_attention_masks=kwargs.pop('attention_mask'),
            text_labels=None,
            pixel_values=kwargs.pop('pixel_values')
        )
        if getattr(self.generation_config, 'cache_implementation') == 'hybrid':  # mainly for Gemma2
            kwargs['past_key_values'] = self.get_llm()._get_cache('hybrid', getattr(kwargs, "num_beams", 1), kwargs['max_new_tokens'] + inputs_embeds.shape[-2])
            self.get_llm()._supports_cache_class = True
            kwargs['cache_implementation'] = None

        return self.llm.generate(inputs=None, inputs_embeds=inputs_embeds, attention_mask=attention_mask, **kwargs)


AutoConfig.register("ovis", OvisConfig)
AutoModelForCausalLM.register(OvisConfig, Ovis)
