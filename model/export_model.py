"""Export ResNet101 as a TensorFlow Serving SavedModel.

The exported model owns its preprocessing and postprocessing so clients never
have to know about Caffe-style mean subtraction or the ImageNet label map:

* ``serving_default``  - input ``image_bytes`` (string[batch]): encoded JPEG/PNG/GIF/BMP.
  Decode, resize the shorter side to 256, centre-crop 224x224, classify.
  This keeps REST payloads small (~20-60 KB per image instead of ~1 MB of JSON
  floats), which matters more than anything else for CPU-bound JSON serving.
* ``predict_pixels``   - input ``images`` (float32[batch, 224, 224, 3]): RGB in [0, 255].

Both signatures return ``classes`` (int32[batch, 5]), ``labels`` (string[batch, 5])
and ``scores`` (float32[batch, 5]), i.e. top-5 instead of 1000 probabilities.

The script also writes ``assets.extra/tf_serving_warmup_requests`` so TF Serving
runs a few inferences before reporting the model AVAILABLE. Without it the first
real requests on every new pod pay for graph optimisation and memory allocation,
which shows up as P99 spikes exactly when the HPA adds capacity.

    python model/export_model.py --output-dir model/resnet101 --version 1
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import keras
import numpy as np
import tensorflow as tf

from download_model import load_class_names, load_resnet101

MODEL_NAME = "resnet101"
IMAGE_SIZE = 224
RESIZE_SHORTER_SIDE = 256
TOP_K = 5
# keras.applications.resnet.preprocess_input ("caffe" mode): RGB -> BGR, then
# subtract the ImageNet channel means. No scaling.
CAFFE_BGR_MEAN = (103.939, 116.779, 123.68)

IMAGE_BYTES_SIGNATURE = "serving_default"
PIXELS_SIGNATURE = "predict_pixels"


def decode_and_resize(image_bytes: tf.Tensor) -> tf.Tensor:
    """Encoded image -> float32[224, 224, 3] RGB in [0, 255]."""
    image = tf.io.decode_image(image_bytes, channels=3, expand_animations=False)
    image.set_shape([None, None, 3])
    height_width = tf.cast(tf.shape(image)[:2], tf.float32)
    scale = RESIZE_SHORTER_SIDE / tf.reduce_min(height_width)
    new_size = tf.maximum(tf.cast(tf.round(height_width * scale), tf.int32), IMAGE_SIZE)
    image = tf.image.resize(image, new_size, method="bilinear", antialias=True)
    return tf.image.resize_with_crop_or_pad(image, IMAGE_SIZE, IMAGE_SIZE)


def build_serving_archive(model: keras.Model, class_names: list[str]) -> keras.export.ExportArchive:
    """Wrap the Keras model with pre/postprocessing as TF Serving signatures.

    ExportArchive (rather than tf.saved_model.save on a tf.Module) is required
    for Keras 3: it tracks the variables so that TF Serving's C++ loader, which
    restores them by name, can find them. A tf.Module export reloads fine in
    Python but fails in TF Serving with "Could not find variable ...".

    Deliberately no ``archive.track(model)``: write_out() already collects
    every variable the endpoints use, and tracking the model as well stores
    each weight twice (342 MB instead of 171 MB, ~250 MiB more RSS per pod).
    """

    def classify(rgb_images: tf.Tensor) -> dict[str, tf.Tensor]:
        bgr_images = tf.reverse(rgb_images, axis=[-1]) - tf.constant(CAFFE_BGR_MEAN)
        probabilities = model(bgr_images, training=False)
        scores, classes = tf.math.top_k(probabilities, k=TOP_K)
        return {
            "classes": classes,
            "labels": tf.gather(tf.constant(class_names), classes),
            "scores": scores,
        }

    def serve_image_bytes(image_bytes: tf.Tensor) -> dict[str, tf.Tensor]:
        images = tf.map_fn(
            decode_and_resize,
            image_bytes,
            fn_output_signature=tf.TensorSpec([IMAGE_SIZE, IMAGE_SIZE, 3], tf.float32),
        )
        return classify(images)

    def serve_pixels(images: tf.Tensor) -> dict[str, tf.Tensor]:
        # TF Serving does not enforce signature shapes, and global average
        # pooling makes ResNet accept any spatial size, so a 10x10 input would
        # silently return garbage. Fail with INVALID_ARGUMENT (HTTP 400).
        images = tf.ensure_shape(images, [None, IMAGE_SIZE, IMAGE_SIZE, 3])
        return classify(images)

    archive = keras.export.ExportArchive()
    archive.add_endpoint(
        name=IMAGE_BYTES_SIGNATURE,
        fn=serve_image_bytes,
        input_signature=[tf.TensorSpec([None], tf.string, name="image_bytes")],
    )
    archive.add_endpoint(
        name=PIXELS_SIGNATURE,
        fn=serve_pixels,
        input_signature=[
            tf.TensorSpec([None, IMAGE_SIZE, IMAGE_SIZE, 3], tf.float32, name="images")
        ],
    )
    return archive


def synthetic_jpeg(seed: int, height: int = 375, width: int = 500) -> bytes:
    """A deterministic JPEG, used for warmup and export verification.

    Smooth colour waves rather than noise: noise does not compress, and every
    warmup image is stored inside the model directory (and so the image).
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width]
    frequency = rng.uniform(0.01, 0.05, size=3)
    phase = rng.uniform(0, 2 * np.pi, size=3)
    channels = [127.5 + 127.5 * np.sin(frequency[c] * (x + y) + phase[c]) for c in range(3)]
    pixels = np.stack(channels, axis=-1).astype(np.uint8)
    return tf.io.encode_jpeg(pixels, quality=90).numpy()


def write_warmup_requests(version_dir: Path) -> int:
    """Write PredictionLog records that TF Serving replays at model load."""
    from tensorflow_serving.apis import predict_pb2, prediction_log_pb2

    def image_bytes_request(batch_size: int) -> predict_pb2.PredictRequest:
        request = predict_pb2.PredictRequest()
        request.model_spec.name = MODEL_NAME
        request.model_spec.signature_name = IMAGE_BYTES_SIGNATURE
        images = [synthetic_jpeg(seed) for seed in range(batch_size)]
        request.inputs["image_bytes"].CopyFrom(tf.make_tensor_proto(images, dtype=tf.string))
        return request

    def pixels_request(batch_size: int) -> predict_pb2.PredictRequest:
        request = predict_pb2.PredictRequest()
        request.model_spec.name = MODEL_NAME
        request.model_spec.signature_name = PIXELS_SIGNATURE
        pixels = np.full((batch_size, IMAGE_SIZE, IMAGE_SIZE, 3), 127.0, dtype=np.float32)
        request.inputs["images"].CopyFrom(tf.make_tensor_proto(pixels))
        return request

    # Cover the batch sizes the load tests use so their kernels are warm too.
    requests = [image_bytes_request(1), image_bytes_request(16), pixels_request(1)]

    warmup_dir = version_dir / "assets.extra"
    warmup_dir.mkdir(parents=True, exist_ok=True)
    with tf.io.TFRecordWriter(str(warmup_dir / "tf_serving_warmup_requests")) as writer:
        for request in requests:
            log = prediction_log_pb2.PredictionLog(
                predict_log=prediction_log_pb2.PredictLog(request=request)
            )
            writer.write(log.SerializeToString())
    return len(requests)


def verify_export(version_dir: Path) -> None:
    """Reload the SavedModel and check both signatures produce sane output."""
    loaded = tf.saved_model.load(str(version_dir))
    missing = {IMAGE_BYTES_SIGNATURE, PIXELS_SIGNATURE} - set(loaded.signatures)
    if missing:
        raise RuntimeError(f"Missing signatures: {missing}")

    batch = tf.constant([synthetic_jpeg(1), synthetic_jpeg(2)])
    outputs = loaded.signatures[IMAGE_BYTES_SIGNATURE](image_bytes=batch)
    pixels = loaded.signatures[PIXELS_SIGNATURE](images=tf.zeros([1, IMAGE_SIZE, IMAGE_SIZE, 3]))
    for name, result, batch_size in (
        (IMAGE_BYTES_SIGNATURE, outputs, 2),
        (PIXELS_SIGNATURE, pixels, 1),
    ):
        for key in ("classes", "labels", "scores"):
            if tuple(result[key].shape) != (batch_size, TOP_K):
                raise RuntimeError(f"{name}/{key} has shape {result[key].shape}")
        scores = result["scores"].numpy()
        if not np.all(np.diff(scores, axis=1) <= 0) or scores.sum(axis=1).max() > 1.0 + 1e-4:
            raise RuntimeError(f"{name} returned invalid scores: {scores}")


def export(output_dir: Path, version: int, warmup: bool = True, force: bool = False) -> Path:
    version_dir = output_dir / str(version)
    if version_dir.exists() and not force:
        raise SystemExit(f"{version_dir} already exists (use --force to overwrite)")

    archive = build_serving_archive(load_resnet101(), load_class_names())

    # Write to a temporary directory and move it into place, so a TF Serving
    # instance polling output_dir never sees a half-written version.
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir, prefix=".export-") as tmp:
        staging = Path(tmp) / str(version)
        archive.write_out(str(staging), verbose=False)
        if warmup:
            count = write_warmup_requests(staging)
            print(f"Wrote {count} warmup requests")
        verify_export(staging)
        if version_dir.exists():
            shutil.rmtree(version_dir)
        staging.rename(version_dir)

    print(f"Exported {MODEL_NAME} version {version} to {version_dir}")
    return version_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=Path("model/resnet101"))
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--no-warmup", action="store_true", help="skip warmup request file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing version")
    args = parser.parse_args()
    export(args.output_dir, args.version, warmup=not args.no_warmup, force=args.force)


if __name__ == "__main__":
    main()
