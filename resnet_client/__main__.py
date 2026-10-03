"""Command-line interface.

python -m resnet_client status  --endpoint http://localhost:8501
python -m resnet_client predict --endpoint http://localhost:8501 locust/images/grace_hopper.jpg
python -m resnet_client payload locust/images --batch-size 16 -o locust/payload16.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

from resnet_client.client import ResNetClient
from resnet_client.payload import build_b64_payload
from resnet_client.preprocessing import preprocess_image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


def _expand_images(paths: list[Path]) -> list[Path]:
    images: list[Path] = []
    for path in paths:
        if path.is_dir():
            images.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES))
        else:
            images.append(path)
    if not images:
        raise SystemExit("no images found")
    return images


def cmd_status(args: argparse.Namespace) -> int:
    client = ResNetClient(args.endpoint, args.model)
    if args.wait:
        client.wait_until_ready(timeout=args.wait)
    print(json.dumps(client.model_status(), indent=2))
    return 0 if client.is_ready() else 1


def cmd_predict(args: argparse.Namespace) -> int:
    client = ResNetClient(args.endpoint, args.model)
    paths = _expand_images(args.images)
    start = time.perf_counter()
    if args.pixels:
        predictions = client.predict_pixels([preprocess_image(p) for p in paths])
    else:
        predictions = client.predict_images([p.read_bytes() for p in paths])
    elapsed_ms = (time.perf_counter() - start) * 1000

    for path, prediction in zip(paths, predictions, strict=True):
        print(path.name)
        for label, score in zip(prediction.labels, prediction.scores, strict=True):
            print(f"  {score:6.1%}  {label}")
    print(f"{len(paths)} image(s) in {elapsed_ms:.0f} ms")
    return 0


def cmd_payload(args: argparse.Namespace) -> int:
    paths = _expand_images(args.images)
    # Cycle through the available images to fill the batch.
    batch = [p.read_bytes() for p in itertools.islice(itertools.cycle(paths), args.batch_size)]
    payload = build_b64_payload(batch)
    args.output.write_text(json.dumps(payload) + "\n")
    print(f"Wrote {args.output} ({args.batch_size} images, {args.output.stat().st_size:,} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resnet_client")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_endpoint(p: argparse.ArgumentParser) -> None:
        p.add_argument("--endpoint", default="http://localhost:8501")
        p.add_argument("--model", default="resnet101")

    status = sub.add_parser("status", help="show model version status")
    add_endpoint(status)
    status.add_argument("--wait", type=float, default=0, help="seconds to wait for AVAILABLE")
    status.set_defaults(func=cmd_status)

    predict = sub.add_parser("predict", help="classify images")
    add_endpoint(predict)
    predict.add_argument("images", nargs="+", type=Path)
    predict.add_argument("--pixels", action="store_true", help="use the predict_pixels signature")
    predict.set_defaults(func=cmd_predict)

    payload = sub.add_parser("payload", help="write a b64 :predict payload for Locust")
    payload.add_argument("images", nargs="+", type=Path)
    payload.add_argument("--batch-size", type=int, default=1)
    payload.add_argument("-o", "--output", type=Path, required=True)
    payload.set_defaults(func=cmd_payload)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
