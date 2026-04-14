from __future__ import annotations

import io

from PIL import Image, ImageOps
from fastapi import HTTPException, UploadFile


async def load_pil_image(upload: UploadFile, max_bytes: int) -> Image.Image:
    """Read an uploaded file into a PIL Image.

    Raises HTTP 413 if the file is too large.
    """

    data = await upload.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Image too large: {len(data)} bytes")

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")
