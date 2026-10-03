"""Preprocessing, payload construction and prediction validation.

The tests at the bottom also exercise the exported SavedModel and the
server-side preprocessing; they need TensorFlow and are skipped without it.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import numpy as np
import pytest
from conftest import SAMPLE_LABEL
from PIL import Image

from resnet_client import (
    IMAGE_SIZE,
    PredictionError,
    build_b64_payload,
    build_pixel_payload,
    parse_predictions,
    preprocess_image,
)


def _encode(image: Image.Image, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


# --- preprocessing ------------------------------------------------------------


def test_image_dimensions(sample_image_path):
    image = preprocess_image(sample_image_path)
    assert image.shape == (IMAGE_SIZE, IMAGE_SIZE, 3)
    assert image.dtype == np.float32
    assert image.min() >= 0.0
    assert image.max() <= 255.0


@pytest.mark.parametrize("mode", ["L", "RGBA", "P"])
def test_non_rgb_images_become_rgb(mode):
    image = Image.new(mode, (300, 200))
    assert preprocess_image(_encode(image)).shape == (IMAGE_SIZE, IMAGE_SIZE, 3)


def test_small_images_are_upscaled():
    image = Image.new("RGB", (32, 20))
    assert preprocess_image(_encode(image)).shape == (IMAGE_SIZE, IMAGE_SIZE, 3)


def test_center_crop_keeps_the_middle():
    # 512x256: red side bands, blue middle half. The 224 px centre crop of the
    # (already 256-high) image must contain only blue.
    image = Image.new("RGB", (512, 256), (255, 0, 0))
    image.paste((0, 0, 255), (128, 0, 384, 256))
    pixels = preprocess_image(_encode(image))
    assert pixels[..., 2].min() > 250
    assert pixels[..., 0].max() < 5


# --- payloads -----------------------------------------------------------------


def test_b64_payload_round_trips(sample_image_bytes):
    payload = build_b64_payload([sample_image_bytes, b"second"])
    assert "signature_name" not in payload
    decoded = [base64.b64decode(instance["b64"]) for instance in payload["instances"]]
    assert decoded == [sample_image_bytes, b"second"]


def test_b64_payload_signature_name():
    assert build_b64_payload([b"x"], signature_name="sig")["signature_name"] == "sig"


@pytest.mark.parametrize("builder", [build_b64_payload, build_pixel_payload])
def test_empty_batches_are_rejected(builder):
    with pytest.raises(ValueError):
        builder([])


def test_pixel_payload_is_integer_rgb():
    image = np.full((IMAGE_SIZE, IMAGE_SIZE, 3), 127.6, dtype=np.float32)
    payload = build_pixel_payload([image])
    assert payload["signature_name"] == "predict_pixels"
    instance = payload["instances"][0]
    assert np.array(instance).shape == (IMAGE_SIZE, IMAGE_SIZE, 3)
    assert instance[0][0] == [128, 128, 128]


def test_pixel_payload_rejects_wrong_shape():
    with pytest.raises(ValueError, match="expected shape"):
        build_pixel_payload([np.zeros((10, 10, 3))])


def test_committed_locust_payload_matches_sample_image(root: Path, sample_image_bytes):
    payload = json.loads((root / "locust" / "test_payload.json").read_text())
    assert len(payload["instances"]) == 1
    assert base64.b64decode(payload["instances"][0]["b64"]) == sample_image_bytes


# --- prediction validation ----------------------------------------------------


def _row(**overrides):
    row = {
        "classes": [652, 834, 513, 917, 566],
        "labels": ["military_uniform", "suit", "cornet", "comic_book", "French_horn"],
        "scores": [0.767, 0.083, 0.032, 0.013, 0.011],
    }
    row.update(overrides)
    return row


def test_parse_valid_predictions():
    predictions = parse_predictions({"predictions": [_row(), _row()]}, expected_count=2)
    assert [p.top1 for p in predictions] == ["military_uniform"] * 2
    assert predictions[0].classes[0] == 652


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"error": "boom"}, "server returned an error"),
        ({"outputs": {}}, "no 'predictions'"),
        ({"predictions": [_row()]}, "expected 2 predictions"),
        ({"predictions": [_row(), {"classes": [1]}]}, "missing"),
        ({"predictions": [_row(), _row(scores=[0.1, 0.5, 0.1, 0.1, 0.1])]}, "not sorted"),
        ({"predictions": [_row(), _row(scores=[0.9, 0.9, 0.1, 0.1, 0.1])]}, "sum to more than 1"),
        ({"predictions": [_row(), _row(classes=[1000, 1, 2, 3, 4])]}, "outside"),
        ({"predictions": [_row(), _row(classes=[1, 1, 2, 3, 4])]}, "duplicate"),
        ({"predictions": [_row(), _row(labels=["a"])]}, "top-5"),
    ],
)
def test_parse_rejects_invalid_predictions(response, message):
    with pytest.raises(PredictionError, match=message):
        parse_predictions(response, expected_count=2)


# --- exported model (TensorFlow) ----------------------------------------------


@pytest.fixture(scope="module")
def tf():
    return pytest.importorskip("tensorflow")


@pytest.fixture(scope="module")
def exported_model(tf, root):
    version_dir = root / "model" / "resnet101" / "1"
    if not (version_dir / "saved_model.pb").exists():
        pytest.skip("model not exported; run `make export-model`")
    return tf.saved_model.load(str(version_dir))


@pytest.mark.tensorflow
def test_client_and_server_preprocessing_agree(tf, sample_image_bytes):
    import export_model
    from resnet_client import preprocessing

    assert export_model.IMAGE_SIZE == preprocessing.IMAGE_SIZE
    assert export_model.RESIZE_SHORTER_SIDE == preprocessing.RESIZE_SHORTER_SIDE

    server = export_model.decode_and_resize(tf.constant(sample_image_bytes)).numpy()
    client = preprocess_image(sample_image_bytes)
    assert server.shape == client.shape
    # PIL and TF resize kernels differ slightly; ~1/255 on average.
    assert np.abs(server - client).mean() < 2.5


@pytest.mark.tensorflow
def test_exported_signatures_classify_sample(tf, exported_model, sample_image_bytes):
    outputs = exported_model.signatures["serving_default"](
        image_bytes=tf.constant([sample_image_bytes, sample_image_bytes])
    )
    assert outputs["classes"].shape == (2, 5)
    assert outputs["labels"].numpy()[0][0].decode() == SAMPLE_LABEL

    pixels = preprocess_image(sample_image_bytes)[np.newaxis]
    outputs = exported_model.signatures["predict_pixels"](images=tf.constant(pixels))
    assert outputs["labels"].numpy()[0][0].decode() == SAMPLE_LABEL


@pytest.mark.tensorflow
def test_weights_are_stored_once(tf, root, exported_model):
    """44.7 M float32 parameters = ~179 MB. A duplicate copy (e.g. from
    ExportArchive.track()) doubles image size and pod memory."""
    variables = root / "model" / "resnet101" / "1" / "variables"
    size = sum(f.stat().st_size for f in variables.iterdir())
    assert size < 200e6, f"variables are {size / 1e6:.0f} MB; weights duplicated?"


@pytest.mark.tensorflow
def test_export_includes_warmup_requests(root, exported_model):
    warmup = root / "model" / "resnet101" / "1" / "assets.extra" / "tf_serving_warmup_requests"
    assert warmup.stat().st_size > 0
