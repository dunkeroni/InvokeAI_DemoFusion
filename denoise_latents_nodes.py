import inspect
from contextlib import ExitStack
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import torch
import torchvision
from diffusers.configuration_utils import ConfigMixin
from diffusers.models.adapter import T2IAdapter
from diffusers.models.unets.unet_2d_condition import UNet2DConditionModel
from diffusers.schedulers.scheduling_tcd import TCDScheduler
from diffusers.schedulers.scheduling_utils import SchedulerMixin as Scheduler
from pydantic import field_validator
from transformers import CLIPVisionModelWithProjection
from invokeai.backend.model_manager import BaseModelType
from invokeai.app.invocations.constants import LATENT_SCALE_FACTOR, SCHEDULER_NAME_VALUES
from invokeai.app.invocations.fields import (
    ConditioningField,
    DenoiseMaskField,
    FieldDescriptions,
    Input,
    Field,
    InputField,
    LatentsField,
    OutputField,
    UIType,
)
from invokeai.app.invocations.ip_adapter import IPAdapterField
from invokeai.app.invocations.primitives import LatentsOutput
from invokeai.app.invocations.t2i_adapter import T2IAdapterField
from invokeai.app.services.shared.invocation_context import InvocationContext
from invokeai.app.util.controlnet_utils import prepare_control_image
from invokeai.backend.ip_adapter.ip_adapter import IPAdapter, IPAdapterPlus
from invokeai.backend.lora import LoRAModelRaw
from invokeai.backend.model_patcher import ModelPatcher
from invokeai.backend.stable_diffusion import PipelineIntermediateState, set_seamless
from invokeai.backend.stable_diffusion.diffusion.conditioning_data import (
    BasicConditioningInfo,
    IPAdapterConditioningInfo,
    IPAdapterData,
    Range,
    SDXLConditioningInfo,
    TextConditioningData,
    TextConditioningRegions,
)
from invokeai.backend.util.mask import to_standard_float_mask
from invokeai.backend.util.silence_warnings import SilenceWarnings

from invokeai.backend.stable_diffusion.diffusers_pipeline import (
    ControlNetData,
    StableDiffusionGeneratorPipeline,
    T2IAdapterData,
)
from .extendable_diffusers_pipeline import ExtendableStableDiffusionGeneratorPipeline
from invokeai.backend.stable_diffusion.schedulers import SCHEDULER_MAP
from invokeai.backend.util.devices import TorchDevice
from invokeai.invocation_api import BaseInvocation, BaseInvocationOutput, invocation, invocation_output
from invokeai.app.invocations.controlnet_image_processors import ControlField
from invokeai.invocation_api import UNetField
from invokeai.app.invocations.latent import get_scheduler

DEFAULT_PRECISION = TorchDevice.choose_torch_dtype()

from pydantic import BaseModel
from .denoise_latents_extensions import (
    GuidanceField,
    DenoiseLatentsInputs,
    DenoiseLatentsData,
    ExtensionHandlerSD12X,
)


class ModuleData(BaseModel):
    name: str = Field(description="user-facing name of the module")
    module_type: str = Field(description="software type of the module")
    module: str = Field(description="Name of the module function")
    module_kwargs: dict | None = Field(description="Keyword arguments to pass to the module function")


@invocation_output("guidance_module_output")
class ModuleDataOutput(BaseInvocationOutput):
    module_data_output: ModuleData | None = OutputField(
        title="Guidance Module",
        description="Information to alter the denoising process"
    )


@invocation(
    "modular_denoise_latents",
    title="Modular Denoise Latents",
    tags=["modular", "latents", "denoise", "txt2img", "t2i", "t2l", "img2img", "i2i", "l2l"],
    category="modular",
    version="2.0.0",
)
class ModularDenoiseLatentsInvocation(BaseInvocation):
    """Denoises noisy latents to decodable images"""

    positive_conditioning: Union[ConditioningField, list[ConditioningField]] = InputField(
        description=FieldDescriptions.positive_cond, input=Input.Connection, ui_order=0
    )
    negative_conditioning: Union[ConditioningField, list[ConditioningField]] = InputField(
        description=FieldDescriptions.negative_cond, input=Input.Connection, ui_order=1
    )
    noise: Optional[LatentsField] = InputField(
        default=None,
        description=FieldDescriptions.noise,
        input=Input.Connection,
        ui_order=3,
    )
    steps: int = InputField(default=10, gt=0, description=FieldDescriptions.steps)
    cfg_scale: Union[float, List[float]] = InputField(
        default=7.5, description=FieldDescriptions.cfg_scale, title="CFG Scale"
    )
    denoising_start: float = InputField(
        default=0.0,
        ge=0,
        le=1,
        description=FieldDescriptions.denoising_start,
    )
    denoising_end: float = InputField(default=1.0, ge=0, le=1, description=FieldDescriptions.denoising_end)
    scheduler: SCHEDULER_NAME_VALUES = InputField(
        default="euler",
        description=FieldDescriptions.scheduler,
        ui_type=UIType.Scheduler,
    )
    unet: UNetField = InputField(
        description=FieldDescriptions.unet,
        input=Input.Connection,
        title="UNet",
        ui_order=2,
    )
    control: Optional[Union[ControlField, list[ControlField]]] = InputField(
        default=None,
        input=Input.Connection,
        ui_order=5,
    )
    ip_adapter: Optional[Union[IPAdapterField, list[IPAdapterField]]] = InputField(
        description=FieldDescriptions.ip_adapter,
        title="IP-Adapter",
        default=None,
        input=Input.Connection,
        ui_order=6,
    )
    t2i_adapter: Optional[Union[T2IAdapterField, list[T2IAdapterField]]] = InputField(
        description=FieldDescriptions.t2i_adapter,
        title="T2I-Adapter",
        default=None,
        input=Input.Connection,
        ui_order=7,
    )
    # cfg_rescale_multiplier: float = InputField(
    #     title="CFG Rescale Multiplier", default=0, ge=0, lt=1, description=FieldDescriptions.cfg_rescale_multiplier
    # )
    latents: Optional[LatentsField] = InputField(
        default=None,
        description=FieldDescriptions.latents,
        input=Input.Connection,
        ui_order=4,
    )
    additional_guidance: Optional[Union[GuidanceField, list[GuidanceField]]] = InputField(
        default=None,
        input=Input.Connection,
        ui_order=8,
    )

    @field_validator("cfg_scale")
    def ge_one(cls, v: Union[List[float], float]) -> Union[List[float], float]:
        """validate that all cfg_scale values are >= 1"""
        if isinstance(v, list):
            for i in v:
                if i < 1:
                    raise ValueError("cfg_scale must be greater than 1")
        else:
            if v < 1:
                raise ValueError("cfg_scale must be greater than 1")
        return v

    def _get_text_embeddings_and_masks(
        self,
        cond_list: list[ConditioningField],
        context: InvocationContext,
        device: torch.device,
        dtype: torch.dtype,
    ) -> tuple[Union[list[BasicConditioningInfo], list[SDXLConditioningInfo]], list[Optional[torch.Tensor]]]:
        """Get the text embeddings and masks from the input conditioning fields."""
        text_embeddings: Union[list[BasicConditioningInfo], list[SDXLConditioningInfo]] = []
        text_embeddings_masks: list[Optional[torch.Tensor]] = []
        for cond in cond_list:
            cond_data = context.conditioning.load(cond.conditioning_name)
            text_embeddings.append(cond_data.conditionings[0].to(device=device, dtype=dtype))

            mask = cond.mask
            if mask is not None:
                mask = context.tensors.load(mask.tensor_name)
            text_embeddings_masks.append(mask)

        return text_embeddings, text_embeddings_masks

    def _preprocess_regional_prompt_mask(
        self, mask: Optional[torch.Tensor], target_height: int, target_width: int, dtype: torch.dtype
    ) -> torch.Tensor:
        """Preprocess a regional prompt mask to match the target height and width.
        If mask is None, returns a mask of all ones with the target height and width.
        If mask is not None, resizes the mask to the target height and width using 'nearest' interpolation.

        Returns:
            torch.Tensor: The processed mask. shape: (1, 1, target_height, target_width).
        """

        if mask is None:
            return torch.ones((1, 1, target_height, target_width), dtype=dtype)

        mask = to_standard_float_mask(mask, out_dtype=dtype)

        tf = torchvision.transforms.Resize(
            (target_height, target_width), interpolation=torchvision.transforms.InterpolationMode.NEAREST
        )

        # Add a batch dimension to the mask, because torchvision expects shape (batch, channels, h, w).
        mask = mask.unsqueeze(0)  # Shape: (1, h, w) -> (1, 1, h, w)
        resized_mask = tf(mask)
        return resized_mask

    def _concat_regional_text_embeddings(
        self,
        text_conditionings: Union[list[BasicConditioningInfo], list[SDXLConditioningInfo]],
        masks: Optional[list[Optional[torch.Tensor]]],
        latent_height: int,
        latent_width: int,
        dtype: torch.dtype,
    ) -> tuple[Union[BasicConditioningInfo, SDXLConditioningInfo], Optional[TextConditioningRegions]]:
        """Concatenate regional text embeddings into a single embedding and track the region masks accordingly."""
        if masks is None:
            masks = [None] * len(text_conditionings)
        assert len(text_conditionings) == len(masks)

        is_sdxl = type(text_conditionings[0]) is SDXLConditioningInfo

        all_masks_are_none = all(mask is None for mask in masks)

        text_embedding = []
        pooled_embedding = None
        add_time_ids = None
        cur_text_embedding_len = 0
        processed_masks = []
        embedding_ranges = []

        for prompt_idx, text_embedding_info in enumerate(text_conditionings):
            mask = masks[prompt_idx]

            if is_sdxl:
                # We choose a random SDXLConditioningInfo's pooled_embeds and add_time_ids here, with a preference for
                # prompts without a mask. We prefer prompts without a mask, because they are more likely to contain
                # global prompt information.  In an ideal case, there should be exactly one global prompt without a
                # mask, but we don't enforce this.

                # HACK(ryand): The fact that we have to choose a single pooled_embedding and add_time_ids here is a
                # fundamental interface issue. The SDXL Compel nodes are not designed to be used in the way that we use
                # them for regional prompting. Ideally, the DenoiseLatents invocation should accept a single
                # pooled_embeds tensor and a list of standard text embeds with region masks. This change would be a
                # pretty major breaking change to a popular node, so for now we use this hack.
                if pooled_embedding is None or mask is None:
                    pooled_embedding = text_embedding_info.pooled_embeds
                if add_time_ids is None or mask is None:
                    add_time_ids = text_embedding_info.add_time_ids

            text_embedding.append(text_embedding_info.embeds)
            if not all_masks_are_none:
                embedding_ranges.append(
                    Range(
                        start=cur_text_embedding_len, end=cur_text_embedding_len + text_embedding_info.embeds.shape[1]
                    )
                )
                processed_masks.append(
                    self._preprocess_regional_prompt_mask(mask, latent_height, latent_width, dtype=dtype)
                )

            cur_text_embedding_len += text_embedding_info.embeds.shape[1]

        text_embedding = torch.cat(text_embedding, dim=1)
        assert len(text_embedding.shape) == 3  # batch_size, seq_len, token_len

        regions = None
        if not all_masks_are_none:
            regions = TextConditioningRegions(
                masks=torch.cat(processed_masks, dim=1),
                ranges=embedding_ranges,
            )

        if is_sdxl:
            return (
                SDXLConditioningInfo(embeds=text_embedding, pooled_embeds=pooled_embedding, add_time_ids=add_time_ids),
                regions,
            )
        return BasicConditioningInfo(embeds=text_embedding), regions

    def get_conditioning_data(
        self,
        context: InvocationContext,
        unet: UNet2DConditionModel,
        latent_height: int,
        latent_width: int,
    ) -> TextConditioningData:
        # Normalize self.positive_conditioning and self.negative_conditioning to lists.
        cond_list = self.positive_conditioning
        if not isinstance(cond_list, list):
            cond_list = [cond_list]
        uncond_list = self.negative_conditioning
        if not isinstance(uncond_list, list):
            uncond_list = [uncond_list]

        cond_text_embeddings, cond_text_embedding_masks = self._get_text_embeddings_and_masks(
            cond_list, context, unet.device, unet.dtype
        )
        uncond_text_embeddings, uncond_text_embedding_masks = self._get_text_embeddings_and_masks(
            uncond_list, context, unet.device, unet.dtype
        )

        cond_text_embedding, cond_regions = self._concat_regional_text_embeddings(
            text_conditionings=cond_text_embeddings,
            masks=cond_text_embedding_masks,
            latent_height=latent_height,
            latent_width=latent_width,
            dtype=unet.dtype,
        )
        uncond_text_embedding, uncond_regions = self._concat_regional_text_embeddings(
            text_conditionings=uncond_text_embeddings,
            masks=uncond_text_embedding_masks,
            latent_height=latent_height,
            latent_width=latent_width,
            dtype=unet.dtype,
        )

        if isinstance(self.cfg_scale, list):
            assert (
                len(self.cfg_scale) == self.steps
            ), "cfg_scale (list) must have the same length as the number of steps"

        conditioning_data = TextConditioningData(
            uncond_text=uncond_text_embedding,
            cond_text=cond_text_embedding,
            uncond_regions=uncond_regions,
            cond_regions=cond_regions,
            guidance_scale=self.cfg_scale,
        )
        return conditioning_data

    def create_pipeline(
        self,
        unet: UNet2DConditionModel,
        scheduler: Scheduler,
    ) -> ExtendableStableDiffusionGeneratorPipeline: #MODIFIED FOR NEW CALL PARAMETERS
        class FakeVae:
            class FakeVaeConfig:
                def __init__(self) -> None:
                    self.block_out_channels = [0]

            def __init__(self) -> None:
                self.config = FakeVae.FakeVaeConfig()

        return ExtendableStableDiffusionGeneratorPipeline(
            vae=FakeVae(),  # TODO: oh...
            text_encoder=None,
            tokenizer=None,
            unet=unet,
            scheduler=scheduler,
            safety_checker=None,
            feature_extractor=None,
            requires_safety_checker=False,
        )

    def prep_control_data(
        self,
        context: InvocationContext,
        control_input: Optional[Union[ControlField, List[ControlField]]],
        latents_shape: List[int],
        exit_stack: ExitStack,
        do_classifier_free_guidance: bool = True,
    ) -> Optional[List[ControlNetData]]:
        # Assuming fixed dimensional scaling of LATENT_SCALE_FACTOR.
        control_height_resize = latents_shape[2] * LATENT_SCALE_FACTOR
        control_width_resize = latents_shape[3] * LATENT_SCALE_FACTOR
        if control_input is None:
            control_list = None
        elif isinstance(control_input, list) and len(control_input) == 0:
            control_list = None
        elif isinstance(control_input, ControlField):
            control_list = [control_input]
        elif isinstance(control_input, list) and len(control_input) > 0 and isinstance(control_input[0], ControlField):
            control_list = control_input
        else:
            control_list = None
        if control_list is None:
            return None
        # After above handling, any control that is not None should now be of type list[ControlField].

        # FIXME: add checks to skip entry if model or image is None
        #        and if weight is None, populate with default 1.0?
        controlnet_data = []
        for control_info in control_list:
            control_model = exit_stack.enter_context(context.models.load(control_info.control_model))

            # control_models.append(control_model)
            control_image_field = control_info.image
            input_image = context.images.get_pil(control_image_field.image_name)
            # self.image.image_type, self.image.image_name
            # FIXME: still need to test with different widths, heights, devices, dtypes
            #        and add in batch_size, num_images_per_prompt?
            #        and do real check for classifier_free_guidance?
            # prepare_control_image should return torch.Tensor of shape(batch_size, 3, height, width)
            control_image = prepare_control_image(
                image=input_image,
                do_classifier_free_guidance=do_classifier_free_guidance,
                width=control_width_resize,
                height=control_height_resize,
                # batch_size=batch_size * num_images_per_prompt,
                # num_images_per_prompt=num_images_per_prompt,
                device=control_model.device,
                dtype=control_model.dtype,
                control_mode=control_info.control_mode,
                resize_mode=control_info.resize_mode,
            )
            control_item = ControlNetData(
                model=control_model,  # model object
                image_tensor=control_image,
                weight=control_info.control_weight,
                begin_step_percent=control_info.begin_step_percent,
                end_step_percent=control_info.end_step_percent,
                control_mode=control_info.control_mode,
                # any resizing needed should currently be happening in prepare_control_image(),
                #    but adding resize_mode to ControlNetData in case needed in the future
                resize_mode=control_info.resize_mode,
            )
            controlnet_data.append(control_item)
            # MultiControlNetModel has been refactored out, just need list[ControlNetData]

        return controlnet_data

    def prep_ip_adapter_data(
        self,
        context: InvocationContext,
        ip_adapter: Optional[Union[IPAdapterField, list[IPAdapterField]]],
        exit_stack: ExitStack,
        latent_height: int,
        latent_width: int,
        dtype: torch.dtype,
    ) -> Optional[list[IPAdapterData]]:
        """If IP-Adapter is enabled, then this function loads the requisite models, and adds the image prompt embeddings
        to the `conditioning_data` (in-place).
        """
        if ip_adapter is None:
            return None

        # ip_adapter could be a list or a single IPAdapterField. Normalize to a list here.
        if not isinstance(ip_adapter, list):
            ip_adapter = [ip_adapter]

        if len(ip_adapter) == 0:
            return None

        ip_adapter_data_list = []
        for single_ip_adapter in ip_adapter:
            ip_adapter_model: Union[IPAdapter, IPAdapterPlus] = exit_stack.enter_context(
                context.models.load(single_ip_adapter.ip_adapter_model)
            )

            image_encoder_model_info = context.models.load(single_ip_adapter.image_encoder_model)
            # `single_ip_adapter.image` could be a list or a single ImageField. Normalize to a list here.
            single_ipa_image_fields = single_ip_adapter.image
            if not isinstance(single_ipa_image_fields, list):
                single_ipa_image_fields = [single_ipa_image_fields]

            single_ipa_images = [context.images.get_pil(image.image_name) for image in single_ipa_image_fields]

            # TODO(ryand): With some effort, the step of running the CLIP Vision encoder could be done before any other
            # models are needed in memory. This would help to reduce peak memory utilization in low-memory environments.
            with image_encoder_model_info as image_encoder_model:
                assert isinstance(image_encoder_model, CLIPVisionModelWithProjection)
                # Get image embeddings from CLIP and ImageProjModel.
                image_prompt_embeds, uncond_image_prompt_embeds = ip_adapter_model.get_image_embeds(
                    single_ipa_images, image_encoder_model
                )

            mask = single_ip_adapter.mask
            if mask is not None:
                mask = context.tensors.load(mask.tensor_name)
            mask = self._preprocess_regional_prompt_mask(mask, latent_height, latent_width, dtype=dtype)

            ip_adapter_data_list.append(
                IPAdapterData(
                    ip_adapter_model=ip_adapter_model,
                    weight=single_ip_adapter.weight,
                    target_blocks=single_ip_adapter.target_blocks,
                    begin_step_percent=single_ip_adapter.begin_step_percent,
                    end_step_percent=single_ip_adapter.end_step_percent,
                    ip_adapter_conditioning=IPAdapterConditioningInfo(image_prompt_embeds, uncond_image_prompt_embeds),
                    mask=mask,
                )
            )

        return ip_adapter_data_list

    def run_t2i_adapters(
        self,
        context: InvocationContext,
        t2i_adapter: Optional[Union[T2IAdapterField, list[T2IAdapterField]]],
        latents_shape: list[int],
        do_classifier_free_guidance: bool,
    ) -> Optional[list[T2IAdapterData]]:
        if t2i_adapter is None:
            return None

        # Handle the possibility that t2i_adapter could be a list or a single T2IAdapterField.
        if isinstance(t2i_adapter, T2IAdapterField):
            t2i_adapter = [t2i_adapter]

        if len(t2i_adapter) == 0:
            return None

        t2i_adapter_data = []
        for t2i_adapter_field in t2i_adapter:
            t2i_adapter_model_config = context.models.get_config(t2i_adapter_field.t2i_adapter_model.key)
            t2i_adapter_loaded_model = context.models.load(t2i_adapter_field.t2i_adapter_model)
            image = context.images.get_pil(t2i_adapter_field.image.image_name)

            # The max_unet_downscale is the maximum amount that the UNet model downscales the latent image internally.
            if t2i_adapter_model_config.base == BaseModelType.StableDiffusion1:
                max_unet_downscale = 8
            elif t2i_adapter_model_config.base == BaseModelType.StableDiffusionXL:
                max_unet_downscale = 4
            else:
                raise ValueError(f"Unexpected T2I-Adapter base model type: '{t2i_adapter_model_config.base}'.")

            t2i_adapter_model: T2IAdapter
            with t2i_adapter_loaded_model as t2i_adapter_model:
                total_downscale_factor = t2i_adapter_model.total_downscale_factor

                # Resize the T2I-Adapter input image.
                # We select the resize dimensions so that after the T2I-Adapter's total_downscale_factor is applied, the
                # result will match the latent image's dimensions after max_unet_downscale is applied.
                t2i_input_height = latents_shape[2] // max_unet_downscale * total_downscale_factor
                t2i_input_width = latents_shape[3] // max_unet_downscale * total_downscale_factor

                # Note: We have hard-coded `do_classifier_free_guidance=False`. This is because we only want to prepare
                # a single image. If CFG is enabled, we will duplicate the resultant tensor after applying the
                # T2I-Adapter model.
                #
                # Note: We re-use the `prepare_control_image(...)` from ControlNet for T2I-Adapter, because it has many
                # of the same requirements (e.g. preserving binary masks during resize).
                t2i_image = prepare_control_image(
                    image=image,
                    do_classifier_free_guidance=False,
                    width=t2i_input_width,
                    height=t2i_input_height,
                    num_channels=t2i_adapter_model.config["in_channels"],  # mypy treats this as a FrozenDict
                    device=t2i_adapter_model.device,
                    dtype=t2i_adapter_model.dtype,
                    resize_mode=t2i_adapter_field.resize_mode,
                )

                adapter_state = t2i_adapter_model(t2i_image)

            if do_classifier_free_guidance:
                for idx, value in enumerate(adapter_state):
                    adapter_state[idx] = torch.cat([value] * 2, dim=0)

            t2i_adapter_data.append(
                T2IAdapterData(
                    adapter_state=adapter_state,
                    weight=t2i_adapter_field.weight,
                    begin_step_percent=t2i_adapter_field.begin_step_percent,
                    end_step_percent=t2i_adapter_field.end_step_percent,
                )
            )

        return t2i_adapter_data

    # original idea by https://github.com/AmericanPresidentJimmyCarter
    # TODO: research more for second order schedulers timesteps
    def init_scheduler(
        self,
        scheduler: Union[Scheduler, ConfigMixin],
        device: torch.device,
        steps: int,
        denoising_start: float,
        denoising_end: float,
        seed: int,
    ) -> Tuple[int, List[int], int, Dict[str, Any]]:
        assert isinstance(scheduler, ConfigMixin)
        if scheduler.config.get("cpu_only", False):
            scheduler.set_timesteps(steps, device="cpu")
            timesteps = scheduler.timesteps.to(device=device)
        else:
            scheduler.set_timesteps(steps, device=device)
            timesteps = scheduler.timesteps

        # skip greater order timesteps
        _timesteps = timesteps[:: scheduler.order]

        # get start timestep index
        t_start_val = int(round(scheduler.config["num_train_timesteps"] * (1 - denoising_start)))
        t_start_idx = len(list(filter(lambda ts: ts >= t_start_val, _timesteps)))

        # get end timestep index
        t_end_val = int(round(scheduler.config["num_train_timesteps"] * (1 - denoising_end)))
        t_end_idx = len(list(filter(lambda ts: ts >= t_end_val, _timesteps[t_start_idx:])))

        # apply order to indexes
        t_start_idx *= scheduler.order
        t_end_idx *= scheduler.order

        init_timestep = timesteps[t_start_idx : t_start_idx + 1]
        timesteps = timesteps[t_start_idx : t_start_idx + t_end_idx]
        num_inference_steps = len(timesteps) // scheduler.order

        scheduler_step_kwargs: Dict[str, Any] = {}
        scheduler_step_signature = inspect.signature(scheduler.step)
        if "generator" in scheduler_step_signature.parameters:
            # At some point, someone decided that schedulers that accept a generator should use the original seed with
            # all bits flipped. I don't know the original rationale for this, but now we must keep it like this for
            # reproducibility.
            scheduler_step_kwargs.update({"generator": torch.Generator(device=device).manual_seed(seed ^ 0xFFFFFFFF)})
        if isinstance(scheduler, TCDScheduler):
            scheduler_step_kwargs.update({"eta": 1.0})

        return num_inference_steps, timesteps, init_timestep, scheduler_step_kwargs

    @torch.no_grad()
    def invoke(self, context: InvocationContext) -> LatentsOutput:
        #Store inputs in a dataclass for extensions to reference
        inputs_data = DenoiseLatentsInputs(
            positive_conditioning = self.positive_conditioning,
            negative_conditioning = self.negative_conditioning,
            noise = self.noise,
            latents = self.latents,
            steps = self.steps,
            cfg_scale = self.cfg_scale,
            denoising_start = self.denoising_start,
            denoising_end = self.denoising_end,
            scheduler = self.scheduler,
            unet = self.unet,
            control = self.control,
            ip_adapter = self.ip_adapter,
            t2i_adapter = self.t2i_adapter,
        )

        #instantiate all extensions
        extension_handler = ExtensionHandlerSD12X(context, self.additional_guidance, inputs_data)

        with SilenceWarnings():  # this quenches NSFW nag from diffusers
            seed = None
            noise = None
            if self.noise is not None:
                noise = context.tensors.load(self.noise.latents_name)
                seed = self.noise.seed

            if self.latents is not None:
                latents = context.tensors.load(self.latents.latents_name)
                if seed is None:
                    seed = self.latents.seed

                if noise is not None and noise.shape[1:] != latents.shape[1:]:
                    raise Exception(f"Incompatable 'noise' and 'latents' shapes: {latents.shape=} {noise.shape=}")

            elif noise is not None:
                latents = torch.zeros_like(noise)
            else:
                raise Exception("'latents' or 'noise' must be provided!")

            if seed is None:
                seed = 0

            # TODO(ryand): I have hard-coded `do_classifier_free_guidance=True` to mirror the behaviour of ControlNets,
            # below. Investigate whether this is appropriate.
            t2i_adapter_data = self.run_t2i_adapters(
                context,
                self.t2i_adapter,
                latents.shape,
                do_classifier_free_guidance=True,
            )

            # get the unet's config so that we can pass the base to dispatch_progress()
            unet_config = context.models.get_config(self.unet.unet.key)

            def step_callback(state: PipelineIntermediateState) -> None:
                context.util.sd_step_callback(state, unet_config.base)

            def _lora_loader() -> Iterator[Tuple[LoRAModelRaw, float]]:
                for lora in self.unet.loras:
                    lora_info = context.models.load(lora.lora)
                    assert isinstance(lora_info.model, LoRAModelRaw)
                    yield (lora_info.model, lora.weight)
                    del lora_info
                return

            unet_info = context.models.load(self.unet.unet)
            assert isinstance(unet_info.model, UNet2DConditionModel)
            with (
                ExitStack() as exit_stack,
                ModelPatcher.apply_freeu(unet_info.model, self.unet.freeu_config),
                set_seamless(unet_info.model, self.unet.seamless_axes),  # FIXME
                extension_handler.call_patches(unet_model=unet_info.model), # Apply extension model patches
                unet_info as unet,
                # Apply the LoRA after unet has been moved to its target device for faster patching.
                ModelPatcher.apply_lora_unet(unet, _lora_loader()),
            ):
                assert isinstance(unet, UNet2DConditionModel)
                data = DenoiseLatentsData( # centralized structure for all data
                    seed=seed,
                    t2i_adapter_data=t2i_adapter_data,
                ) 
                data.latents = latents.to(device=unet.device, dtype=unet.dtype)
                if noise is not None:
                    data.noise = noise.to(device=unet.device, dtype=unet.dtype)

                data.scheduler = get_scheduler(
                    context=context,
                    scheduler_info=self.unet.scheduler,
                    scheduler_name=self.scheduler,
                    seed=seed,
                )

                data.pipeline = self.create_pipeline(unet, data.scheduler)

                _, _, latent_height, latent_width = latents.shape
                data.conditioning_data = self.get_conditioning_data(
                    context=context, unet=unet, latent_height=latent_height, latent_width=latent_width
                )

                data.controlnet_data = self.prep_control_data(
                    context=context,
                    control_input=self.control,
                    latents_shape=latents.shape,
                    # do_classifier_free_guidance=(self.cfg_scale >= 1.0))
                    do_classifier_free_guidance=True,
                    exit_stack=exit_stack,
                )

                data.ip_adapter_data = self.prep_ip_adapter_data(
                    context=context,
                    ip_adapter=self.ip_adapter,
                    exit_stack=exit_stack,
                    latent_height=latent_height,
                    latent_width=latent_width,
                    dtype=unet.dtype,
                )

                data.num_inference_steps, data.timesteps, data.init_timestep, data.scheduler_step_kwargs = self.init_scheduler(
                    data.scheduler,
                    device=unet.device,
                    steps=self.steps,
                    denoising_start=self.denoising_start,
                    denoising_end=self.denoising_end,
                    seed=seed,
                )

                extension_handler.call_modifiers("modify_data_before_denoising", data=data)

                result_latents = data.pipeline.latents_from_embeddings(
                    data=data,
                    extension_handler=extension_handler,
                    callback=step_callback,
                )

            # https://discuss.huggingface.co/t/memory-usage-by-later-pipeline-stages/23699
            result_latents = result_latents.to("cpu")
            TorchDevice.empty_cache()

            name = context.tensors.save(tensor=result_latents)
        return LatentsOutput.build(latents_name=name, latents=result_latents, seed=None)
