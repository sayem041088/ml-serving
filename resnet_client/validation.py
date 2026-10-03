"""Validate and parse ``:predict`` responses."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

TOP_K = 5
NUM_CLASSES = 1000
_SCORE_TOLERANCE = 1e-4


class PredictionError(ValueError):
    """The server answered, but the response is not a valid classification."""


@dataclass(frozen=True)
class Prediction:
    classes: list[int]
    labels: list[str]
    scores: list[float]

    @property
    def top1(self) -> str:
        return self.labels[0]


def _validate_instance(index: int, row: object) -> Prediction:
    if not isinstance(row, dict):
        raise PredictionError(f"prediction {index} is not an object: {row!r}")
    missing = {"classes", "labels", "scores"} - row.keys()
    if missing:
        raise PredictionError(f"prediction {index} is missing {sorted(missing)}")

    classes, labels, scores = row["classes"], row["labels"], row["scores"]
    if not len(classes) == len(labels) == len(scores) == TOP_K:
        raise PredictionError(f"prediction {index} does not contain top-{TOP_K} results")
    if any(not isinstance(c, int) or not 0 <= c < NUM_CLASSES for c in classes):
        raise PredictionError(f"prediction {index} has class ids outside [0, {NUM_CLASSES})")
    if len(set(classes)) != TOP_K:
        raise PredictionError(f"prediction {index} has duplicate classes: {classes}")
    if any(not 0.0 <= s <= 1.0 + _SCORE_TOLERANCE for s in scores):
        raise PredictionError(f"prediction {index} has scores outside [0, 1]: {scores}")
    if any(a < b for a, b in pairwise(scores)):
        raise PredictionError(f"prediction {index} scores are not sorted: {scores}")
    if sum(scores) > 1.0 + _SCORE_TOLERANCE:
        raise PredictionError(f"prediction {index} scores sum to more than 1: {scores}")
    return Prediction(classes=list(classes), labels=list(labels), scores=list(scores))


def parse_predictions(response: object, expected_count: int | None = None) -> list[Prediction]:
    """Parse a row-format ``{"predictions": [...]}`` response, raising PredictionError."""
    if not isinstance(response, dict):
        raise PredictionError(f"response is not a JSON object: {response!r}")
    if "error" in response:
        raise PredictionError(f"server returned an error: {response['error']}")
    predictions = response.get("predictions")
    if not isinstance(predictions, list):
        raise PredictionError("response has no 'predictions' list")
    if expected_count is not None and len(predictions) != expected_count:
        raise PredictionError(f"expected {expected_count} predictions, got {len(predictions)}")
    return [_validate_instance(i, row) for i, row in enumerate(predictions)]
