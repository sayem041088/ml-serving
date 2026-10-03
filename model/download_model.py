"""Download the ImageNet-pretrained ResNet101 weights and validate the model.

Keras caches the weights (and verifies their MD5 hash) under ~/.keras/models, so
running this once makes ``export_model.py`` work offline afterwards.

    python model/download_model.py
"""

from __future__ import annotations

import json

import keras

CLASS_INDEX_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/data/imagenet_class_index.json"
)
CLASS_INDEX_MD5 = "c2c37ea517e94d9795004a39431a14cb"

EXPECTED_INPUT_SHAPE = (None, 224, 224, 3)
EXPECTED_OUTPUT_SHAPE = (None, 1000)


def load_class_names() -> list[str]:
    """Return the 1000 ImageNet class names ordered by class index."""
    path = keras.utils.get_file(
        "imagenet_class_index.json",
        CLASS_INDEX_URL,
        cache_subdir="models",
        file_hash=CLASS_INDEX_MD5,
    )
    with open(path) as f:
        index = json.load(f)
    return [index[str(i)][1] for i in range(len(index))]


def load_resnet101() -> keras.Model:
    model = keras.applications.ResNet101(weights="imagenet")
    if tuple(model.input_shape) != EXPECTED_INPUT_SHAPE:
        raise RuntimeError(f"Unexpected input shape {model.input_shape}")
    if tuple(model.output_shape) != EXPECTED_OUTPUT_SHAPE:
        raise RuntimeError(f"Unexpected output shape {model.output_shape}")
    return model


def main() -> None:
    model = load_resnet101()
    class_names = load_class_names()
    print(f"Input shape:  {model.input_shape}")
    print(f"Output shape: {model.output_shape}")
    print(f"Parameters:   {model.count_params():,}")
    print(f"Classes:      {len(class_names)} (e.g. {class_names[:3]})")


if __name__ == "__main__":
    main()
