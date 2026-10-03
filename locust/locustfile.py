"""Locust load test for the ResNet101 TensorFlow Serving endpoint.

Interactive (web UI on :8089):
    locust -f locust/locustfile.py --host http://EXTERNAL_IP

Headless staged test (see profiles.py), as used by scripts/load_test.sh:
    LOAD_PROFILE=staged locust -f locust/locustfile.py --headless --host http://EXTERNAL_IP

Environment variables:
    PAYLOAD_FILE        request body (default: test_payload.json next to this file)
    BATCH_SIZE          repeat the payload's instances to this many images per request
    LOAD_PROFILE        name of a staged profile in profiles.py; unset = use -u/-r
    USER_SCALE          multiply every stage's user count (default 1.0)
    WAIT_MIN/WAIT_MAX   think time per user in seconds (default 5 / 15, see below)
    RECONNECT_EVERY     send "Connection: close" every N requests per user (default 1, 0 = never)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from locust import FastHttpUser, LoadTestShape, between, events, task
from profiles import PROFILES

HERE = Path(__file__).resolve().parent
MODEL_NAME = os.getenv("MODEL_NAME", "resnet101")
PREDICT_PATH = f"/v1/models/{MODEL_NAME}:predict"
PAYLOAD_FILE = Path(os.getenv("PAYLOAD_FILE", HERE / "test_payload.json"))
# Each simulated user classifies one image every ~10 s. ResNet101 on CPU is
# expensive (~5 images/s per 2-CPU pod, measured), so with the 0.1-1 s think
# time of a typical web test even the 10-user baseline would saturate two pods.
# At ~10 s the staged profile's 10 -> 500 users offers ~1 -> ~50 req/s: idle,
# then HPA scale-out, then node scale-out, then saturation.
WAIT_MIN = float(os.getenv("WAIT_MIN", "5"))
WAIT_MAX = float(os.getenv("WAIT_MAX", "15"))
RECONNECT_EVERY = int(os.getenv("RECONNECT_EVERY", "1"))
LOAD_PROFILE = os.getenv("LOAD_PROFILE", "")
USER_SCALE = float(os.getenv("USER_SCALE", "1.0"))

# Read and serialise the payload once at start-up. Every simulated user sends
# the same bytes, so the load generator spends its CPU on requests rather than
# on disk reads and JSON encoding.
_payload = json.loads(PAYLOAD_FILE.read_text())
if os.getenv("BATCH_SIZE"):
    _instances = _payload["instances"]
    _payload["instances"] = [
        _instances[i % len(_instances)] for i in range(int(os.environ["BATCH_SIZE"]))
    ]
BATCH_SIZE = len(_payload["instances"])
PAYLOAD_BYTES = json.dumps(_payload).encode()
HEADERS = {"Content-Type": "application/json"}
CLOSE_HEADERS = {**HEADERS, "Connection": "close"}
REQUEST_NAME = f"predict batch={BATCH_SIZE}"


class ResNetUser(FastHttpUser):
    wait_time = between(WAIT_MIN, WAIT_MAX)
    # TF Serving's own REST timeout is 30 s; give it a little longer so server
    # timeouts are reported as such rather than as client-side aborts.
    network_timeout = 35.0
    connection_timeout = 10.0

    def on_start(self) -> None:
        self.request_count = 0

    @task
    def predict(self) -> None:
        # The Service is an L4 load balancer: it balances TCP connections, not
        # requests. With keep-alive, users stay pinned to the pods that existed
        # when they connected. Measured on kind with reconnects every 20
        # requests: an original pod served 1291 predictions, a pod added by the
        # HPA served 2 - while the HPA saw a healthy 66% *average* and stopped
        # scaling. At ~10 s think time a new connection per request is cheap
        # and lets kube-proxy spread every request.
        self.request_count += 1
        reconnect = RECONNECT_EVERY > 0 and self.request_count % RECONNECT_EVERY == 0
        with self.client.post(
            PREDICT_PATH,
            data=PAYLOAD_BYTES,
            headers=CLOSE_HEADERS if reconnect else HEADERS,
            name=REQUEST_NAME,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {(response.text or '')[:200]}")
                return
            try:
                predictions = response.json()["predictions"]
            except (ValueError, KeyError, TypeError):
                response.failure("response is not a valid predictions object")
                return
            if len(predictions) != BATCH_SIZE:
                response.failure(f"expected {BATCH_SIZE} predictions, got {len(predictions)}")


if LOAD_PROFILE:
    if LOAD_PROFILE not in PROFILES:
        raise SystemExit(f"Unknown LOAD_PROFILE {LOAD_PROFILE!r}; choose from {sorted(PROFILES)}")

    class StagedLoadShape(LoadTestShape):
        """Hold each (duration, users, spawn_rate) stage, then stop."""

        stages = tuple(
            (duration, max(1, round(users * USER_SCALE)), spawn_rate)
            for duration, users, spawn_rate in PROFILES[LOAD_PROFILE]
        )

        def tick(self):
            elapsed = 0
            for duration, users, spawn_rate in self.stages:
                elapsed += duration
                if self.get_run_time() < elapsed:
                    return users, spawn_rate
            return None


@events.test_start.add_listener
def _log_configuration(environment, **_kwargs) -> None:
    print(
        f"payload={PAYLOAD_FILE.name} batch={BATCH_SIZE} bytes={len(PAYLOAD_BYTES):,} "
        f"profile={LOAD_PROFILE or 'none'} user_scale={USER_SCALE} "
        f"wait=[{WAIT_MIN}, {WAIT_MAX}]s reconnect_every={RECONNECT_EVERY}"
    )


@events.quitting.add_listener
def _log_images_per_second(environment, **_kwargs) -> None:
    total = environment.stats.total
    if total.num_requests:
        print(
            f"images/s={total.total_rps * BATCH_SIZE:.1f} "
            f"(requests/s={total.total_rps:.1f} x batch {BATCH_SIZE}), "
            f"failures={total.fail_ratio:.2%}"
        )
