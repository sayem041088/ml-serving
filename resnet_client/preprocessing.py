"""Client-side preprocessing for the ``predict_pixels`` signature.

Mirrors ``decode_and_resize`` in model/export_model.py: shorter side resized to
256, centre crop to 224x224, RGB, values in [0, 255]. Mean subtraction stays
inside the model so the image-bytes and pixel paths cannot drift apart.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

IMAGE_SIZE = 224
RESIZE_SHORTER_SIDE = 256

ImageSource = str | Path | bytes


def load_image(source: ImageSource) -> Image.Image:
    """Open a path or encoded bytes as an RGB image (drops alpha, expands greyscale)."""
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
        return image.convert("RGB")


def resize_and_center_crop(
    image: Image.Image, size: int = IMAGE_SIZE, shorter_side: int = RESIZE_SHORTER_SIDE
) -> Image.Image:
    width, height = image.size
    scale = shorter_side / min(width, height)
    new_width = max(size, round(width * scale))
    new_height = max(size, round(height * scale))
    image = image.resize((new_width, new_height), Image.Resampling.BILINEAR)
    left = (new_width - size) // 2
    top = (new_height - size) // 2
    return image.crop((left, top, left + size, top + size))


def preprocess_image(source: ImageSource) -> np.ndarray:
    """Return a float32 array of shape (224, 224, 3), RGB, values in [0, 255]."""
    return np.asarray(resize_and_center_crop(load_image(source)), dtype=np.float32)
