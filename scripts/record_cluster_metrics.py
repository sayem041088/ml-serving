#!/usr/bin/env python3
"""Sample HPA, pod and node state to CSV while a load test runs.

    scripts/record_cluster_metrics.py --output results/run/cluster.csv --interval 10

Locust only sees the client side. This records the cluster side on the same
clock (Unix seconds) so scripts/summarize_results.py can join the two.
Uses kubectl only; stops on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

FIELDS = [
    "timestamp",
    "hpa_current_replicas",
    "hpa_desired_replicas",
    "hpa_cpu_utilization",
    "ready_pods",
    "pending_pods",
    "nodes",
    "ready_nodes",
    "pod_cpu_millicores",
    "pod_memory_mib",
]

_running = True


def _stop(*_args) -> None:
    global _running
    _running = False


def kubectl(*args: str) -> str:
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout


def kubectl_json(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json"))


def _parse_cpu(value: str) -> float:
    if value.endswith("n"):
        return int(value[:-1]) / 1e6
    if value.endswith("u"):
        return int(value[:-1]) / 1e3
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000


def _parse_memory_mib(value: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    return float(value) / 2**20


def pod_usage(namespace: str, selector: str) -> tuple[float, float]:
    """Total CPU (millicores) and memory (MiB) from the metrics API."""
    path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods?labelSelector={selector}"
    usage = json.loads(kubectl("get", "--raw", path))
    cpu = memory = 0.0
    for pod in usage.get("items", []):
        for container in pod.get("containers", []):
            cpu += _parse_cpu(container["usage"]["cpu"])
            memory += _parse_memory_mib(container["usage"]["memory"])
    return cpu, memory


def _is_ready(obj: dict) -> bool:
    conditions = obj["status"].get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def sample(namespace: str, hpa: str, selector: str, node_selector: str = "") -> dict:
    row: dict = {"timestamp": int(time.time())}

    status = kubectl_json("-n", namespace, "get", "hpa", hpa).get("status", {})
    row["hpa_current_replicas"] = status.get("currentReplicas", "")
    row["hpa_desired_replicas"] = status.get("desiredReplicas", "")
    row["hpa_cpu_utilization"] = ""
    for metric in status.get("currentMetrics") or []:
        resource = metric.get("resource", {})
        if resource.get("name") == "cpu":
            row["hpa_cpu_utilization"] = resource.get("current", {}).get("averageUtilization", "")

    pods = kubectl_json("-n", namespace, "get", "pods", "-l", selector)["items"]
    row["pending_pods"] = sum(p["status"].get("phase") == "Pending" for p in pods)
    row["ready_pods"] = sum(
        _is_ready(p) for p in pods if not p["metadata"].get("deletionTimestamp")
    )

    node_args = ["-l", node_selector] if node_selector else []
    nodes = kubectl_json("get", "nodes", *node_args)["items"]
    row["nodes"] = len(nodes)
    row["ready_nodes"] = sum(_is_ready(n) for n in nodes)

    try:
        cpu, memory = pod_usage(namespace, selector)
        row["pod_cpu_millicores"], row["pod_memory_mib"] = round(cpu), round(memory)
    except (subprocess.SubprocessError, KeyError, ValueError):
        row["pod_cpu_millicores"] = row["pod_memory_mib"] = ""
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--namespace", default="ml-serving")
    parser.add_argument("--hpa", default="resnet-hpa")
    parser.add_argument("--selector", default="app=resnet-serving")
    parser.add_argument(
        "--node-selector",
        default="",
        help="count only matching nodes, e.g. cloud.google.com/gke-nodepool=ml-pool",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        while _running:
            started = time.monotonic()
            try:
                writer.writerow(sample(args.namespace, args.hpa, args.selector, args.node_selector))
                f.flush()
            except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as exc:
                print(f"record_cluster_metrics: sample failed: {exc}", file=sys.stderr)
            # Sleep in small steps so SIGTERM stops us promptly.
            while _running and time.monotonic() - started < args.interval:
                time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
