"""Thin HTTP client for the TensorFlow Serving REST API."""

from __future__ import annotations

import time
from collections.abc import Sequence

import numpy as np
import requests

from resnet_client.payload import build_b64_payload, build_pixel_payload
from resnet_client.validation import Prediction, parse_predictions

DEFAULT_MODEL = "resnet101"
METRICS_PATH = "/monitoring/prometheus/metrics"


class ResNetClient:
    def __init__(
        self,
        endpoint: str,
        model_name: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def model_url(self) -> str:
        return f"{self.endpoint}/v1/models/{self.model_name}"

    def model_status(self) -> dict:
        response = self.session.get(self.model_url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def metadata(self) -> dict:
        response = self.session.get(f"{self.model_url}/metadata", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def is_ready(self) -> bool:
        try:
            status = self.model_status()
        except requests.RequestException:
            return False
        versions = status.get("model_version_status", [])
        return any(v.get("state") == "AVAILABLE" for v in versions)

    def wait_until_ready(self, timeout: float = 300.0, interval: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.is_ready():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.model_url} not AVAILABLE after {timeout:.0f}s")
            time.sleep(interval)

    def predict_raw(self, payload: dict) -> requests.Response:
        """POST a payload as-is, without raising on HTTP errors."""
        return self.session.post(f"{self.model_url}:predict", json=payload, timeout=self.timeout)

    def predict_images(self, images: Sequence[bytes]) -> list[Prediction]:
        """Classify encoded images (JPEG/PNG/...) via the default signature."""
        response = self.predict_raw(build_b64_payload(images))
        response.raise_for_status()
        return parse_predictions(response.json(), expected_count=len(images))

    def predict_pixels(self, images: Sequence[np.ndarray]) -> list[Prediction]:
        """Classify preprocessed (224, 224, 3) RGB arrays via ``predict_pixels``."""
        response = self.predict_raw(build_pixel_payload(images))
        response.raise_for_status()
        return parse_predictions(response.json(), expected_count=len(images))

    def prometheus_metrics(self) -> str:
        response = self.session.get(f"{self.endpoint}{METRICS_PATH}", timeout=self.timeout)
        response.raise_for_status()
        return response.text
