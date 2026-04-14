from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class NormalizedBBox:
    x1: float
    y1: float
    x2: float
    y2: float


def pad_bbox(b: NormalizedBBox, pad: float) -> NormalizedBBox:
    """Pad a normalized bbox by `pad` ratio of its width/height."""

    w = b.x2 - b.x1
    h = b.y2 - b.y1
    px = w * pad
    py = h * pad
    x1 = max(0.0, b.x1 - px)
    y1 = max(0.0, b.y1 - py)
    x2 = min(1.0, b.x2 + px)
    y2 = min(1.0, b.y2 + py)
    if x2 <= x1:
        x2 = min(1.0, x1 + 1e-6)
    if y2 <= y1:
        y2 = min(1.0, y1 + 1e-6)
    return NormalizedBBox(x1=x1, y1=y1, x2=x2, y2=y2)


def crop_from_bbox(image: Image.Image, b: NormalizedBBox) -> Image.Image:
    """Crop a PIL image using a normalized bbox."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    w, h = image.size
    left = int(round(b.x1 * w))
    upper = int(round(b.y1 * h))
    right = int(round(b.x2 * w))
    lower = int(round(b.y2 * h))

    left = max(0, min(w - 1, left))
    upper = max(0, min(h - 1, upper))
    right = max(left + 1, min(w, right))
    lower = max(upper + 1, min(h, lower))
    return image.crop((left, upper, right, lower))
