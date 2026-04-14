from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn.functional as F


@dataclass(frozen=True)
class ModelSpec:
    name: str
    input_size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]


def get_model_spec(model_name: str, input_size_override: Optional[int] = None) -> ModelSpec:
    """Return preprocessing parameters for supported models."""

    name = model_name.lower().strip()
    if name == "dinov2":
        size = 518
    elif name == "clip":
        size = 336
    elif name == "mega-l":
        size = 384
    elif name == "mega-l-224":
        size = 224
    elif name == "mega-t":
        size = 224
    elif name == "miewid":
        # Your evaluation code used 440x440.
        size = 440
    else:
        raise ValueError(f"Unsupported model_name: {model_name}")

    if input_size_override is not None:
        size = int(input_size_override)

    # Defaults based on your PoC scripts.
    mean = (0.485, 0.456, 0.406)
    std = (0.229, 0.224, 0.225)
    return ModelSpec(name=name, input_size=size, mean=mean, std=std)


def preprocess_batch(images_0_1: torch.Tensor, spec: ModelSpec) -> torch.Tensor:
    """Preprocess a batch of images.

    Args:
        images_0_1: (N,3,H,W) float32 in range [0,1]
        spec: ModelSpec

    Returns:
        Tensor (N,3,spec.input_size,spec.input_size) normalized.
    """

    if images_0_1.ndim != 4 or images_0_1.shape[1] != 3:
        raise ValueError(f"Expected (N,3,H,W), got {tuple(images_0_1.shape)}")

    x = F.interpolate(
        images_0_1,
        size=(spec.input_size, spec.input_size),
        mode="bilinear",
        align_corners=False,
    )

    mean = torch.tensor(spec.mean, device=x.device, dtype=x.dtype).view(1, 3, 1, 1)
    std = torch.tensor(spec.std, device=x.device, dtype=x.dtype).view(1, 3, 1, 1)
    x = (x - mean) / std
    return x
