from .modular_decorators import module_noise_pred, get_noise_prediction_module
from .modular_denoise_latents import Modular_StableDiffusionGeneratorPipeline, ModuleData, ModuleDataOutput

from invokeai.backend.stable_diffusion.diffusion.conditioning_data import ConditioningData
from invokeai.backend.stable_diffusion.diffusers_pipeline import ControlNetData, T2IAdapterData
from invokeai.app.invocations.primitives import LatentsField
import torch
from typing import Literal, Optional, Callable, List

from invokeai.app.invocations.baseinvocation import (
    BaseInvocation,
    BaseInvocationOutput,
    Input,
    InputField,
    InvocationContext,
    OutputField,
    UIType,
    WithMetadata,
    WithWorkflow,
    invocation,
    invocation_output,
)


def resolve_module(module_dict: dict | None) -> tuple[Callable, dict]:
    """Resolve a module from a module dict. Handles None case automatically. """
    if module_dict is None:
        return get_noise_prediction_module(None), {}
    else:
        return get_noise_prediction_module(module_dict["module"]), module_dict["module_kwargs"]


####################################################################################################
# Standard UNet Step Module
####################################################################################################
"""
Fallback module for the noise prediction pipeline. This module is used when no other module is specified.
"""
@module_noise_pred("standard_unet_step_module")
def standard_do_unet_step(
    self: Modular_StableDiffusionGeneratorPipeline,
    latents: torch.Tensor,
    sample: torch.Tensor,
    t: torch.Tensor,
    conditioning_data: ConditioningData,
    step_index: int,
    total_step_count: int,
    control_data: List[ControlNetData] = None,
    t2i_adapter_data: list[T2IAdapterData] = None,
    **kwargs,
) -> torch.Tensor:
        

        # Handle ControlNet(s)
        down_block_additional_residuals = None
        mid_block_additional_residual = None
        down_intrablock_additional_residuals = None
        if control_data is not None:
            down_block_additional_residuals, mid_block_additional_residual = self.invokeai_diffuser.do_controlnet_step(
                control_data=control_data,
                sample=sample,
                timestep=t[0],
                step_index=step_index,
                total_step_count=total_step_count,
                conditioning_data=conditioning_data,
            )
        
        # and T2I-Adapter(s)
        down_intrablock_additional_residuals = self.get_t2i_intrablock(t2i_adapter_data, step_index, total_step_count)

        # result from calling object's default pipeline
        # extra kwargs get dropped here, so pass whatever you like down the chain
        uc_noise_pred, c_noise_pred = self.invokeai_diffuser.do_unet_step(
            sample=sample,
            timestep=t,
            conditioning_data=conditioning_data,
            step_index=step_index,
            total_step_count=total_step_count,
            down_block_additional_residuals=down_block_additional_residuals,  # for ControlNet
            mid_block_additional_residual=mid_block_additional_residual,  # for ControlNet
            down_intrablock_additional_residuals=down_intrablock_additional_residuals,  # for T2I-Adapter
        )

        guidance_scale = conditioning_data.guidance_scale
        if isinstance(guidance_scale, list):
            guidance_scale = guidance_scale[step_index]

        noise_pred = self.invokeai_diffuser._combine(
            uc_noise_pred,
            c_noise_pred,
            guidance_scale,
        )

        # compute the previous noisy sample x_t -> x_t-1
        step_output = self.scheduler.step(noise_pred, t[0], latents, **conditioning_data.scheduler_args)
        # decrement the index counter, since this might not be the only parallel module
        self.scheduler._step_index -= 1
        # if self.scheduler.order == 2:
        #     self.scheduler._index_counter[t[0].item()] -= 1
        #     print(t[0].item())
        #     print(self.scheduler._index_counter[t[0].item()])

        return step_output

@invocation("standard_unet_step_module",
    title="Standard UNet Step Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class StandardStepModuleInvocation(BaseInvocation):
    """Module: InvokeAI standard noise prediction."""
    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Standard UNet Step Module",
            module_type="do_unet_step",
            module="standard_unet_step_module",
            module_kwargs={},
        )

        return ModuleDataOutput(
            module_data_output=module,
        )


####################################################################################################
# MultiDiffusion Sampling
# From: https://multidiffusion.github.io/
####################################################################################################
import random
import torch.nn.functional as F
def get_views(height, width, window_size=128, stride=64, random_jitter=False):
    # Here, we define the mappings F_i (see Eq. 7 in the MultiDiffusion paper https://arxiv.org/abs/2302.08113)
    # if panorama's height/width < window_size, num_blocks of height/width should return 1
    num_blocks_height = int((height - window_size) / stride - 1e-6) + 2 if height > window_size else 1
    num_blocks_width = int((width - window_size) / stride - 1e-6) + 2 if width > window_size else 1
    total_num_blocks = int(num_blocks_height * num_blocks_width)
    views = []
    for i in range(total_num_blocks):
        h_start = int((i // num_blocks_width) * stride)
        h_end = h_start + window_size
        w_start = int((i % num_blocks_width) * stride)
        w_end = w_start + window_size

        if h_end > height:
            h_start = int(h_start + height - h_end)
            h_end = int(height)
        if w_end > width:
            w_start = int(w_start + width - w_end)
            w_end = int(width)
        if h_start < 0:
            h_end = int(h_end - h_start)
            h_start = 0
        if w_start < 0:
            w_end = int(w_end - w_start)
            w_start = 0

        if random_jitter:
            jitter_range = (window_size - stride) // 4
            w_jitter = 0
            h_jitter = 0
            if (w_start != 0) and (w_end != width):
                w_jitter = random.randint(-jitter_range, jitter_range)
            elif (w_start == 0) and (w_end != width):
                w_jitter = random.randint(-jitter_range, 0)
            elif (w_start != 0) and (w_end == width):
                w_jitter = random.randint(0, jitter_range)
            if (h_start != 0) and (h_end != height):
                h_jitter = random.randint(-jitter_range, jitter_range)
            elif (h_start == 0) and (h_end != height):
                h_jitter = random.randint(-jitter_range, 0)
            elif (h_start != 0) and (h_end == height):
                h_jitter = random.randint(0, jitter_range)
            h_start += (h_jitter + jitter_range)
            h_end += (h_jitter + jitter_range)
            w_start += (w_jitter + jitter_range)
            w_end += (w_jitter + jitter_range)
        
        views.append((int(h_start), int(h_end), int(w_start), int(w_end)))
    return views

def crop_residuals(residual: List | torch.Tensor | None, view: tuple[int, int, int, int]):
    if residual is None:
        print("residual is None")
        return None
    if isinstance(residual, list):
        print(f"list of residuals: {len(residual)}")
        return [crop_residuals(r, view) for r in residual]
    else:
        h_start, h_end, w_start, w_end = view
        print(f"new residual shape: {residual[:, :, h_start:h_end, w_start:w_end].shape}")
        return residual[:, :, h_start:h_end, w_start:w_end]

@module_noise_pred("multidiffusion_sampling")
def multidiffusion_sampling(
    self: Modular_StableDiffusionGeneratorPipeline,
    latents: torch.Tensor,
    sample: torch.Tensor,
    t: torch.Tensor,
    conditioning_data,  # TODO: type
    step_index: int,
    total_step_count: int,
    module_kwargs: dict | None,
    control_data: List[ControlNetData] = None,
    t2i_adapter_data: list[T2IAdapterData] = None,
    **kwargs,
) -> torch.Tensor:
    latent_model_input = sample
    height = latent_model_input.shape[2]
    width = latent_model_input.shape[3]
    window_size = module_kwargs["tile_size"] // 8
    stride = module_kwargs["stride"] // 8
    pad_mode = module_kwargs["pad_mode"]
    enable_jitter = module_kwargs["enable_jitter"]
    sub_module, sub_module_kwargs = resolve_module(module_kwargs["sub_module"])

    views = get_views(height, width, stride=stride, window_size=window_size, random_jitter=enable_jitter)
    if enable_jitter:
        jitter_range = (window_size - stride) // 4
        latents_pad = F.pad(latent_model_input, (jitter_range, jitter_range, jitter_range, jitter_range), pad_mode, 0)
        original_latents_pad = F.pad(latents, (jitter_range, jitter_range, jitter_range, jitter_range), pad_mode, 0)

    else:
        jitter_range = 0
        latents_pad = latent_model_input

    count_local = torch.zeros_like(latents_pad)
    value_local = torch.zeros_like(latents_pad)
    pred_local = torch.zeros_like(latents_pad)
    pred_count_local = torch.zeros_like(latents_pad)
    
    for j, view in enumerate(views):
        h_start, h_end, w_start, w_end = view
        latents_for_view = latents_pad[:, :, h_start:h_end, w_start:w_end]
        cropped_original_latents = original_latents_pad[:, :, h_start:h_end, w_start:w_end]

        _control_data = None
        # crop control data list into tiles
        if control_data is not None:
            _control_data = []
            for c in control_data:
                if enable_jitter:
                    _image_tensor = F.pad(c.image_tensor, (jitter_range*8, jitter_range*8, jitter_range*8, jitter_range*8), pad_mode, 0)
                else:
                    _image_tensor = c.image_tensor
                _control_data.append(ControlNetData(
                    model=c.model,
                    image_tensor=_image_tensor[:, :, h_start*8:h_end*8, w_start*8:w_end*8], #control tensor is in image space
                    weight=c.weight,
                    begin_step_percent=c.begin_step_percent,
                    end_step_percent=c.end_step_percent,
                    control_mode=c.control_mode,
                    resize_mode=c.resize_mode,
                ))
        
        # crop t2i adapter data list into tiles
        _t2i_adapter_data: list[T2IAdapterData] = []
        if t2i_adapter_data is not None:

            for a in t2i_adapter_data:
                tensorlist = a.adapter_state #for some reason this is a List and not a dict, despite what the class definition says!
                _tensorlist = []
                for tensor in tensorlist:
                    scale = height // tensor.shape[2] # SDXL and 1.5 handle differently, one will be 1/2, 1/2, 1/4, 1/4, the other is 1/1, 1/2, 1/4, 1/8
                    if enable_jitter:
                        #hopefully 8 is a common factor of your jitter range and your latent size...
                        _tensor = F.pad(tensor, (jitter_range//scale, jitter_range//scale, jitter_range//scale, jitter_range//scale), pad_mode, 0)
                    else:
                        _tensor = tensor
                    _tensorlist.append(_tensor[:, :, h_start//scale:h_end//scale, w_start//scale:w_end//scale])
                _t2i_adapter_data.append(T2IAdapterData(
                    adapter_state=_tensorlist,
                    weight=a.weight,
                    begin_step_percent=a.begin_step_percent,
                    end_step_percent=a.end_step_percent,
                ))

        step_output = sub_module(
            self=self,
            latents=cropped_original_latents,
            sample=latents_for_view,
            t=t,
            conditioning_data=conditioning_data,
            step_index=step_index,
            total_step_count=total_step_count,
            module_kwargs=sub_module_kwargs,
            control_data=_control_data,
            t2i_adapter_data=_t2i_adapter_data,
            **kwargs,
        )

        # get prediction from output
        prev_sample = step_output.prev_sample.detach().clone()
        value_local[:, :, h_start:h_end, w_start:w_end] += prev_sample #step_output.prev_sample.detach().clone()
        count_local[:, :, h_start:h_end, w_start:w_end] += 1

        pred_original_sample = step_output.pred_original_sample.detach().clone()
        pred_local[:, :, h_start:h_end, w_start:w_end] += pred_original_sample
        pred_count_local[:, :, h_start:h_end, w_start:w_end] += 1


    value_local_crop = value_local[: ,:, jitter_range: jitter_range + height, jitter_range: jitter_range + width]
    count_local_crop = count_local[: ,:, jitter_range: jitter_range + height, jitter_range: jitter_range + width]

    combined_prev_sample = (value_local_crop / count_local_crop)

    pred_local_crop = pred_local[: ,:, jitter_range: jitter_range + height, jitter_range: jitter_range + width]
    pred_count_local_crop = pred_count_local[: ,:, jitter_range: jitter_range + height, jitter_range: jitter_range + width]


    combined_pred_sample = (pred_local_crop / pred_count_local_crop)

    #reinsert combined residuals to output
    step_output.prev_sample = combined_prev_sample
    step_output.pred_original_sample = combined_pred_sample

    return step_output


MD_PAD_MODES = Literal[
    "constant",
    "reflect",
    "replicate",
]

@invocation("multidiffusion_sampling_module",
    title="MultiDiffusion Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class MultiDiffusionSamplingModuleInvocation(BaseInvocation):
    """Module: MultiDiffusion tiled sampling. NOT compatible with t2i adapters."""
    sub_module: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for each noise prediction tile. No connection will use the default pipeline.",
        title="SubModules",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    tile_size: int = InputField(
        title="Tile Size",
        description="Size of the tiles during noise prediction",
        ge=128,
        default=512,
        multiple_of=8,
    )
    stride: int = InputField(
        title="Stride",
        description="The spacing between the starts of tiles during noise prediction (recommend=tile_size/2)",
        ge=64,
        default=256,
        multiple_of=64,
    )
    pad_mode: MD_PAD_MODES = InputField(
        title="Padding Mode",
        description="Padding mode for extending the borders of the latent",
        default="reflect",
        input=Input.Direct,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="MultiDiffusion Sampling Step module",
            module_type="do_unet_step",
            module="multidiffusion_sampling",
            module_kwargs={
                "sub_module": self.sub_module,
                "tile_size": self.tile_size,
                "stride": self.stride,
                "pad_mode": self.pad_mode,
                "enable_jitter": True,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )

####################################################################################################
# Dilated Sampling
# From: https://ruoyidu.github.io/demofusion/demofusion.html
####################################################################################################
def gaussian_kernel(kernel_size=3, sigma=1.0, channels=3):
    x_coord = torch.arange(kernel_size)
    gaussian_1d = torch.exp(-(x_coord - (kernel_size - 1) / 2) ** 2 / (2 * sigma ** 2))
    gaussian_1d = gaussian_1d / gaussian_1d.sum()
    gaussian_2d = gaussian_1d[:, None] * gaussian_1d[None, :]
    kernel = gaussian_2d[None, None, :, :].repeat(channels, 1, 1, 1)
    
    return kernel

def gaussian_filter(latents, kernel_size=3, sigma=1.0):
    channels = latents.shape[1]
    kernel = gaussian_kernel(kernel_size, sigma, channels).to(latents.device, latents.dtype)
    blurred_latents = F.conv2d(latents, kernel, padding=kernel_size//2, groups=channels)
    
    return blurred_latents

@module_noise_pred("dilated_sampling")
def dilated_sampling(
    self: Modular_StableDiffusionGeneratorPipeline,
    sample: torch.Tensor,
    t: torch.Tensor,
    conditioning_data,  # TODO: type
    step_index: int,
    total_step_count: int,
    module_kwargs: dict | None,
    control_data: List[ControlNetData] = None, #prevent from being passed in kwargs
    t2i_adapter_data: list[T2IAdapterData] = None, #prevent from being passed in kwargs
    **kwargs,
) -> torch.Tensor:
    latent_model_input = sample
    gaussian_decay_rate = module_kwargs["gaussian_decay_rate"]
    dilation_scale = module_kwargs["dilation_scale"]
    cosine_factor = 0.5 * (1 + torch.cos(torch.pi * (self.scheduler.config.num_train_timesteps - t) / self.scheduler.config.num_train_timesteps)).cpu()
    sigma = cosine_factor ** gaussian_decay_rate + 1e-2

    sub_module, sub_module_kwargs = resolve_module(module_kwargs["sub_module"])
        
    total_noise_pred = torch.zeros_like(latent_model_input)
    std_, mean_ = latent_model_input.std(), latent_model_input.mean()
    blurred_latents = gaussian_filter(latent_model_input, kernel_size=(2*dilation_scale-1), sigma=sigma)
    blurred_latents = (blurred_latents - blurred_latents.mean()) / blurred_latents.std() * std_ + mean_
    for h in range(dilation_scale):
        for w in range(dilation_scale):
            #get interlaced subsample
            subsample = blurred_latents[:, :, h::dilation_scale, w::dilation_scale]
            noise_pred = sub_module(
                self=self,
                sample=subsample,
                t=t,
                conditioning_data=conditioning_data,
                step_index=step_index,
                total_step_count=total_step_count,
                module_kwargs=sub_module_kwargs,
                control_data=None,
                t2i_adapter_data=None,
                **kwargs,
            )

            # insert subsample noise prediction into total tensor
            total_noise_pred[:, :, h::dilation_scale, w::dilation_scale] = noise_pred
    return total_noise_pred

@invocation("dilated_sampling_module",
    title="Dilated Sampling Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class DilatedSamplingModuleInvocation(BaseInvocation):
    """Module: Dilated Sampling"""
    sub_module: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for each interlaced noise prediction. No connection will use the default pipeline.",
        title="SubModules",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    dilation_scale: int = InputField(
        title="Dilation Factor",
        description="The dilation scale to use when creating interlaced latents (e.g. '2' will split every 2x2 square among 4 latents)",
        ge=1,
        default=2,
    )
    gaussian_decay_rate: float = InputField(
        title="Gaussian Decay Rate",
        description="The decay rate to use when blurring the combined latents. Higher values will result in more blurring in later timesteps.",
        ge=0,
        default=1,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Dilated Sampling Step module",
            module_type="do_unet_step",
            module="dilated_sampling",
            module_kwargs={
                "sub_module": self.sub_module,
                "dilation_scale": self.dilation_scale,
                "gaussian_decay_rate": self.gaussian_decay_rate,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )

####################################################################################################
# Transfer Function: Cosine Decay
# From: https://ruoyidu.github.io/demofusion/demofusion.html
####################################################################################################
@module_noise_pred("cosine_decay_transfer")
def cosine_decay_transfer(
    self: Modular_StableDiffusionGeneratorPipeline,
    t: torch.Tensor,
    module_kwargs: dict | None,
    **kwargs,
) -> torch.Tensor:
    decay_rate = module_kwargs["decay_rate"]
    sub_module_1, sub_module_1_kwargs = resolve_module(module_kwargs["sub_module_1"])
    sub_module_2, sub_module_2_kwargs = resolve_module(module_kwargs["sub_module_2"])

    cosine_factor = 0.5 * (1 + torch.cos(torch.pi * (self.scheduler.config.num_train_timesteps - t) / self.scheduler.config.num_train_timesteps))
    c2 = 1 - cosine_factor ** decay_rate

    pred_1 = sub_module_1(
        self=self,
        t=t,
        module_kwargs=sub_module_1_kwargs,
        **kwargs,
    )

    pred_2 = sub_module_2(
        self=self,
        t=t,
        module_kwargs=sub_module_2_kwargs,
        **kwargs,
    )
    total_noise_pred = torch.lerp(pred_1, pred_2, c2.to(pred_1.device, dtype=pred_1.dtype))

    return total_noise_pred

@invocation("cosine_decay_transfer_module",
    title="Cosine Decay Transfer",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class CosineDecayTransferModuleInvocation(BaseInvocation):
    """Module: Smoothly changed modules based on remaining denoise"""
    sub_module_1: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for the first noise prediction. No connection will use the default pipeline.",
        title="SubModule 1",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    sub_module_2: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for the second noise prediction. No connection will use the default pipeline.",
        title="SubModule 2",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    decay_rate: float = InputField(
        title="Cosine Decay Rate",
        description="The decay rate to use when combining the two noise predictions. Higher values will shift the balance towards the second noise prediction sooner",
        ge=0,
        default=1,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Cosine Decay Transfer module",
            module_type="do_unet_step",
            module="cosine_decay_transfer",
            module_kwargs={
                "sub_module_1": self.sub_module_1,
                "sub_module_2": self.sub_module_2,
                "decay_rate": self.decay_rate,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )

####################################################################################################
# Transfer Function: Linear
####################################################################################################
@module_noise_pred("linear_transfer")
def linear_transfer(
    self: Modular_StableDiffusionGeneratorPipeline,
    t: torch.Tensor,
    module_kwargs: dict | None,
    **kwargs,
) -> torch.Tensor:
    start_step: int = module_kwargs["start_step"]
    end_step: int = module_kwargs["end_step"]
    sub_module_1, sub_module_1_kwargs = resolve_module(module_kwargs["sub_module_1"])
    sub_module_2, sub_module_2_kwargs = resolve_module(module_kwargs["sub_module_2"])
    
    linear_factor = (step_index - start_step) / (end_step - start_step)
    linear_factor = min(max(linear_factor, 0), 1)

    if linear_factor < 1:
        pred_1 = sub_module_1(
            self=self,
            t=t,
            module_kwargs=sub_module_1_kwargs,
            **kwargs,
        )

    if linear_factor > 0:
        pred_2 = sub_module_2(
            self=self,
            t=t,
            module_kwargs=sub_module_2_kwargs,
            **kwargs,
        )
    
    if linear_factor == 0:
        total_noise_pred = pred_1 # no need to lerp
        print(f"Linear Transfer: pred_1")
    elif linear_factor == 1:
        total_noise_pred = pred_2 # no need to lerp
        print(f"Linear Transfer: pred_2")
    else:
        total_noise_pred = torch.lerp(pred_1, pred_2, linear_factor)
        print(f"Linear Transfer: lerp(pred_1, pred_2, {linear_factor})")

    return total_noise_pred

@invocation("linear_transfer_module",
    title="Linear Transfer",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class LinearTransferModuleInvocation(BaseInvocation):
    """Module: Smoothly change modules based on step."""
    sub_module_1: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for the first noise prediction. No connection will use the default pipeline.",
        title="SubModule 1",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    sub_module_2: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for the second noise prediction. No connection will use the default pipeline.",
        title="SubModule 2",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    start_step: int = InputField(
        title="Start Step",
        description="The step index at which to start using the second noise prediction",
        ge=0,
        default=0,
    )
    end_step: int = InputField(
        title="End Step",
        description="The step index at which to stop using the first noise prediction",
        ge=0,
        default=10,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Linear Transfer module",
            module_type="do_unet_step",
            module="linear_transfer",
            module_kwargs={
                "sub_module_1": self.sub_module_1,
                "sub_module_2": self.sub_module_2,
                "start_step": self.start_step,
                "end_step": self.end_step,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )

####################################################################################################
# Tiled Denoise Latents
####################################################################################################
#Doesn't have it's own module function, relies on MultiDiffusion Sampling with jitter disabled.

@invocation("tiled_denoise_latents_module",
    title="Tiled Denoise Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class TiledDenoiseLatentsModuleInvocation(BaseInvocation):
    """Module: Denoise latents using tiled noise prediction"""
    sub_module: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for each noise prediction tile. No connection will use the default pipeline.",
        title="SubModules",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    tile_size: int = InputField(
        title="Tile Size",
        description="Size of the tiles during noise prediction",
        ge=128,
        default=512,
        multiple_of=8,
    )
    overlap: int = InputField(
        title="Overlap",
        description="The minimum amount of overlap between tiles during noise prediction",
        ge=0,
        default=64,
        multiple_of=8,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Tiled Denoise Latents module",
            module_type="do_unet_step",
            module="multidiffusion_sampling",
            module_kwargs={
                "sub_module": self.sub_module,
                "tile_size": self.tile_size,
                "stride": self.tile_size - self.overlap,
                "enable_jitter": False,
                "pad_mode": None,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )

####################################################################################################
# SDXL Color Guidance
# From: https://huggingface.co/blog/TimothyAlexisVass/explaining-the-sdxl-latent-space
####################################################################################################

# Shrinking towards the mean (will also remove outliers)
def soft_clamp_tensor(input_tensor: torch.Tensor, threshold=0.9, boundary=4, channels=[0, 1, 2]):
    for channel in channels:
        channel_tensor = input_tensor[:, channel]
        if not max(abs(channel_tensor.max()), abs(channel_tensor.min())) < 4:
            max_val = channel_tensor.max()
            max_replace = ((channel_tensor - threshold) / (max_val - threshold)) * (boundary - threshold) + threshold
            over_mask = (channel_tensor > threshold)

            min_val = channel_tensor.min()
            min_replace = ((channel_tensor + threshold) / (min_val + threshold)) * (-boundary + threshold) - threshold
            under_mask = (channel_tensor < -threshold)

            input_tensor[:, channel] = torch.where(over_mask, max_replace, torch.where(under_mask, min_replace, channel_tensor))

    return input_tensor

# Center tensor (balance colors)
def center_tensor(input_tensor, channel_shift=1, full_shift=1, channels=[0, 1, 2, 3]):
    for channel in channels:
        input_tensor[0, channel] -= input_tensor[0, channel].mean() * channel_shift
    return input_tensor - input_tensor.mean() * full_shift

# Maximize/normalize tensor
def maximize_tensor(input_tensor, boundary=8, channels=[0, 1, 2]):
    for channel in channels:
        input_tensor[0, channel] *= boundary / input_tensor[0, channel].max()
        #min_val = input_tensor[0, channel].min()
        #max_val = input_tensor[0, channel].max()

        #get min max from 3 standard deviations from mean instead
        mean = input_tensor[0, channel].mean()
        std = input_tensor[0, channel].std()
        min_val = mean - std * 2
        max_val = mean + std * 2

        #colors will always center around 0 for SDXL latents, but brightness/structure will not. Need to adjust this.
        normalization_factor = boundary / max(abs(min_val), abs(max_val))
        input_tensor[0, channel] *= normalization_factor

    return input_tensor

@module_noise_pred("color_guidance")
def color_guidance(
    self: Modular_StableDiffusionGeneratorPipeline,
    t: torch.Tensor,
    module_kwargs: dict | None,
    **kwargs,
) -> torch.Tensor:
    sub_module, sub_module_kwargs = resolve_module(module_kwargs["sub_module"])
    # upper_bound: float = module_kwargs["upper_bound"]
    # lower_bound: float = module_kwargs["lower_bound"]
    # shift_strength: float = module_kwargs["shift_strength"]
    channels = module_kwargs["channels"]
    # expand_dynamic_range: bool = module_kwargs["expand_dynamic_range"]
    timestep: float = t.item()

    noise_pred: torch.Tensor = sub_module(
        self=self,
        t=t,
        module_kwargs=sub_module_kwargs,
        **kwargs,
    )

    # center = upper_bound * 0.5 + lower_bound * 0.5
    # print(f"Color Guidance: timestep={timestep}, center={center}, channels={channels}, lower_bound={lower_bound}, upper_bound={upper_bound}")
    # if timestep > 950:
    #     threshold = max(noise_pred.max(), abs(noise_pred.min())) * 0.998
    #     noise_pred = soft_clamp_tensor(noise_pred, threshold*0.998, threshold)
    # if timestep > 700:
    #     noise_pred = center_tensor(noise_pred, 0.8, channels=channels, center=center)
    # if timestep > 1: #do not shift again after the last step is completed
    #     noise_pred = center_tensor(noise_pred, shift_strength, channels=channels, center=center)
    #     noise_pred = normalize_tensor(noise_pred, lower_bound=lower_bound, upper_bound=upper_bound, channels=channels, expand_dynamic_range=expand_dynamic_range)
    # return noise_pred

    # if timestep > 950:
    #     threshold = max(noise_pred.max(), abs(noise_pred.min())) * 0.998
    #     noise_pred = soft_clamp_tensor(noise_pred, threshold*0.998, threshold,channels=channels)
    if timestep > 700:
        noise_pred = center_tensor(noise_pred, 0.8, 0.8, channels=channels)
    if timestep > 1 and timestep < 100:
        noise_pred = center_tensor(noise_pred, 0.6, 1.0, channels=channels)
        # noise_pred = maximize_tensor(noise_pred, channels=channels)
    return noise_pred

CHANNEL_SELECTIONS = Literal[
    "All Channels",
    "Colors Only",
    "L0: Brightness",
    "L1: Red->Cyan",
    "L2: Magenta->Green",
    "L3: Structure",
]

CHANNEL_VALUES = {
    "All Channels": [0, 1, 2, 3],
    "Colors Only": [1, 2],
    "L0: Brightness": [0],
    "L1: Cyan->Red": [1],
    "L2: Lime->Purple": [2],
    "L3: Structure": [3],
}

@invocation("color_guidance_module",
    title="Color Guidance Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.1",
)
class ColorGuidanceModuleInvocation(BaseInvocation):
    """Module: Color Guidance (fix SDXL yellow bias)"""
    sub_module: Optional[ModuleData] = InputField(
        default=None,
        description="The custom module to use for each noise prediction tile. No connection will use the default pipeline.",
        title="SubModules",
        input=Input.Connection,
        ui_type=UIType.Any,
    )
    # adjustment: float = InputField(
    #     title="Adjustment",
    #     description="0: Will correct colors to remain within VAE bounds. Othervalues will shift the mean of the latent. Recommended range: -0.2->0.2",
    #     default=0,
    # )
    # shift_strength: float = InputField(
    #     title="Shift Strength",
    #     description="How much to shift the latent towards the new mean each step.",
    #     default=0.5,
    # )
    channel_selection: CHANNEL_SELECTIONS = InputField(
        title="Channel Selection",
        description="The channels to affect in the latent correction",
        default="All Channels",
        input=Input.Direct,
    )
    # expand_dynamic_range: bool = InputField(
    #     title="Expand Dynamic Range",
    #     description="If true, will expand the dynamic range of the latent channels to match the range of the VAE. Recommend FALSE when adjustment is not 0",
    #     default=True,
    #     input=Input.Direct,
    # )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:

        channels = CHANNEL_VALUES[self.channel_selection]

        module = ModuleData(
            name="Color Guidance module",
            module_type="do_unet_step",
            module="color_guidance",
            module_kwargs={
                "sub_module": self.sub_module,
                # "upper_bound": 4 + self.adjustment,
                # "lower_bound": -4 + self.adjustment,
                # "shift_strength": self.shift_strength,
                "channels": channels,
                # "expand_dynamic_range": self.expand_dynamic_range,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )


####################################################################################################
# Skip Residual 
# From: https://ruoyidu.github.io/demofusion/demofusion.html
####################################################################################################
import time
"""Instead of denoising, synthetically noise an input latent to the noise level of the current timestep."""
@module_noise_pred("skip_residual")
def skip_residual(
    self: Modular_StableDiffusionGeneratorPipeline,
    sample: torch.Tensor, #just to get the device
    t: torch.Tensor,
    module_kwargs: dict | None,
    **kwargs,
) -> torch.Tensor:
    latents_input: dict = module_kwargs["latent_input"] #gets serialized into a dict instead of a LatentsField for some reason
    noise_input: dict = module_kwargs["noise_input"] #gets serialized into a dict instead of a LatentsField for some reason
    module_id = module_kwargs["module_id"]

    #latents and noise are retrieved and stored in the pipeline object to avoid loading them from disk every step
    persistent_latent = self.check_persistent_data(module_id, "latent") #the latent from the original input on the module
    persistent_noise = self.check_persistent_data(module_id, "noise") #the noise from the original input on the module
    if persistent_latent is None: #load on first call
        persistent_latent = self.context.services.latents.get(latents_input["latents_name"]).to(sample.device)
        self.set_persistent_data(module_id, "latent", persistent_latent)
    if persistent_noise is None: #load on first call
        persistent_noise = self.context.services.latents.get(noise_input["latents_name"]).to(sample.device)
        self.set_persistent_data(module_id, "noise", persistent_noise)
    print(t)
    print(self.scheduler.config.num_train_timesteps)
    print(((t) / self.scheduler.config.num_train_timesteps).item())
    noised_latents = torch.lerp(persistent_latent, persistent_noise, ((t) / self.scheduler.config.num_train_timesteps).item())
    #wait 200ms
    time.sleep(0.2)
    return noised_latents - persistent_latent

@invocation("skip_residual_module",
    title="Skip Residual Module",
    tags=["module", "modular"],
    category="modular",
    version="1.0.0",
)
class SkipResidualModuleInvocation(BaseInvocation):
    """Module: Skip Residual"""
    latent_input: LatentsField = InputField(
        title="Latent Input",
        description="The base latent to use for the noise prediction (usually the same as the input for img2img)",
        input=Input.Connection,
    )
    noise_input: LatentsField = InputField(
        title="Noise Input",
        description="The noise to add to the latent for the noise prediction (usually the same as the noise on the denoise latents node)",
        input=Input.Connection,
    )

    def invoke(self, context: InvocationContext) -> ModuleDataOutput:
        module = ModuleData(
            name="Skip Residual module",
            module_type="do_unet_step",
            module="skip_residual",
            module_kwargs={
                "latent_input": self.latent_input,
                "noise_input": self.noise_input,
                "module_id": self.id,
            },
        )

        return ModuleDataOutput(
            module_data_output=module,
        )
