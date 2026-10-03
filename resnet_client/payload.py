"""Build TensorFlow Serving REST ``:predict`` request bodies."""

from __future__ import annotations

import base64
from collections.abc import Sequence

import numpy as np

from resnet_client.preprocessing import IMAGE_SIZE

PIXELS_SIGNATURE = "predict_pixels"


def build_b64_payload(images: Sequence[bytes], signature_name: str | None = None) -> dict:
    """Payload for the default signature: one base64-encoded image per instance.

    TF Serving decodes ``{"b64": ...}`` objects into DT_STRING tensors.
    """
    if not images:
        raise ValueError("at least one image is required")
    payload: dict = {
        "instances": [{"b64": base64.b64encode(image).decode("ascii")} for image in images]
    }
    if signature_name:
        payload["signature_name"] = signature_name
    return payload


def build_pixel_payload(images: Sequence[np.ndarray]) -> dict:
    """Payload for ``predict_pixels``: preprocessed (224, 224, 3) RGB arrays.

    Pixels are sent as integers: they come from 8-bit images anyway, and the
    JSON is ~40% smaller than with floats. Still ~0.5 MB per image, which is why
    the default signature takes encoded bytes instead.
    """
    if not images:
        raise ValueError("at least one image is required")
    instances = []
    for image in images:
        if image.shape != (IMAGE_SIZE, IMAGE_SIZE, 3):
            raise ValueError(f"expected shape {(IMAGE_SIZE, IMAGE_SIZE, 3)}, got {image.shape}")
        instances.append(np.clip(np.rint(image), 0, 255).astype(np.uint8).tolist())
    return {"signature_name": PIXELS_SIGNATURE, "instances": instances}
