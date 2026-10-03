from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_IMAGE = ROOT / "locust" / "images" / "grace_hopper.jpg"
# ImageNet class 652; ResNet101 gives it ~75% on the sample image.
SAMPLE_LABEL = "military_uniform"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def sample_image_path() -> Path:
    return SAMPLE_IMAGE


@pytest.fixture(scope="session")
def sample_image_bytes() -> bytes:
    return SAMPLE_IMAGE.read_bytes()


@pytest.fixture(scope="session")
def endpoint() -> str:
    """Base URL of a running TF Serving instance, e.g. http://localhost:8501."""
    value = os.getenv("RESNET_ENDPOINT")
    if not value:
        pytest.skip("RESNET_ENDPOINT not set")
    return value.rstrip("/")


@pytest.fixture(scope="session")
def client(endpoint):
    from resnet_client import ResNetClient

    client = ResNetClient(endpoint)
    client.wait_until_ready(timeout=float(os.getenv("RESNET_READY_TIMEOUT", "120")))
    return client
