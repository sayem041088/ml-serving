"""Prediction API tests against a running server.

RESNET_ENDPOINT=http://localhost:8501 pytest -m integration
"""

from __future__ import annotations

import pytest
import requests
from conftest import SAMPLE_LABEL

from resnet_client import build_b64_payload, parse_predictions, preprocess_image

pytestmark = pytest.mark.integration


def test_prediction_endpoint(client, sample_image_bytes):
    response = client.predict_raw(build_b64_payload([sample_image_bytes]))
    assert response.status_code == 200
    (prediction,) = parse_predictions(response.json(), expected_count=1)
    assert prediction.top1 == SAMPLE_LABEL
    assert prediction.scores[0] > 0.5


def test_batch_prediction_returns_one_result_per_image(client, sample_image_bytes):
    predictions = client.predict_images([sample_image_bytes] * 4)
    assert len(predictions) == 4
    assert all(p == predictions[0] for p in predictions)


def test_pixel_signature_matches_image_signature(client, sample_image_bytes):
    (from_pixels,) = client.predict_pixels([preprocess_image(sample_image_bytes)])
    (from_bytes,) = client.predict_images([sample_image_bytes])
    assert from_pixels.top1 == from_bytes.top1 == SAMPLE_LABEL


def test_invalid_image_returns_400(client):
    response = client.predict_raw(build_b64_payload([b"not an image"]))
    assert response.status_code == 400
    assert "error" in response.json()


def test_malformed_json_returns_400(client):
    response = client.session.post(
        f"{client.model_url}:predict",
        data=b'{"instances": [',
        headers={"Content-Type": "application/json"},
        timeout=client.timeout,
    )
    assert response.status_code == 400


def test_wrong_pixel_shape_returns_400(client):
    payload = {"signature_name": "predict_pixels", "instances": [[[[0, 0, 0]] * 10] * 10]}
    assert client.predict_raw(payload).status_code == 400


def test_unknown_signature_returns_400(client, sample_image_bytes):
    payload = build_b64_payload([sample_image_bytes], signature_name="does_not_exist")
    assert client.predict_raw(payload).status_code == 400


def test_unknown_model_returns_404(endpoint):
    response = requests.get(f"{endpoint}/v1/models/does-not-exist", timeout=10)
    assert response.status_code == 404
