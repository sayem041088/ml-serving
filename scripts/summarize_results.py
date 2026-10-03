#!/usr/bin/env python3
"""Turn a load-test run directory into the per-stage results table.

    scripts/summarize_results.py results/20261003T120000Z-staged-b1

Reads locust_stats_history.csv (client side) and, if present, cluster.csv from
scripts/record_cluster_metrics.py (server side), and writes summary.md and
summary.json into the run directory.

Stages are detected as plateaus of constant user count lasting at least
--min-stage seconds; ramps between them are ignored.
- RPS and error rate are exact (deltas of Locust's cumulative counters).
- P50/P95/P99 are the median of Locust's rolling ~10 s percentiles over the
  second half of each stage, i.e. after autoscaling has had time to react.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Stage:
    index: int
    users: int
    start: int
    end: int
    duration_s: int
    rps: float
    images_per_s: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    error_pct: float
    max_ready_pods: int | None = None
    max_desired_pods: int | None = None
    max_nodes: int | None = None
    avg_cpu_pct: float | None = None
    max_pending_pods: int | None = None
    avg_memory_mib_per_pod: float | None = None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.median(present), 1) if present else None


def read_history(path: Path) -> tuple[list[dict], int]:
    with path.open() as f:
        rows = list(csv.DictReader(f))
    batch_size = 1
    for row in rows:
        match = re.search(r"batch=(\d+)", row.get("Name", ""))
        if match:
            batch_size = int(match.group(1))
            break
    aggregated = [r for r in rows if r.get("Name") == "Aggregated"]
    aggregated.sort(key=lambda r: int(r["Timestamp"]))
    return aggregated, batch_size


def read_cluster(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def find_plateaus(rows: list[dict], min_stage_s: int) -> list[list[dict]]:
    plateaus: list[list[dict]] = []
    for row in rows:
        if plateaus and plateaus[-1][0]["User Count"] == row["User Count"]:
            plateaus[-1].append(row)
        else:
            plateaus.append([row])
    return [
        p
        for p in plateaus
        if int(p[-1]["Timestamp"]) - int(p[0]["Timestamp"]) >= min_stage_s
        and int(p[0]["User Count"]) > 0
    ]


def summarize_stage(index: int, rows: list[dict], batch_size: int, cluster: list[dict]) -> Stage:
    first, last = rows[0], rows[-1]
    start, end = int(first["Timestamp"]), int(last["Timestamp"])
    elapsed = max(end - start, 1)
    requests = int(last["Total Request Count"]) - int(first["Total Request Count"])
    failures = int(last["Total Failure Count"]) - int(first["Total Failure Count"])
    rps = requests / elapsed
    steady = rows[len(rows) // 2 :]

    stage = Stage(
        index=index,
        users=int(first["User Count"]),
        start=start,
        end=end,
        duration_s=elapsed,
        rps=round(rps, 2),
        images_per_s=round(rps * batch_size, 2),
        p50_ms=_median([_float(r["50%"]) for r in steady]),
        p95_ms=_median([_float(r["95%"]) for r in steady]),
        p99_ms=_median([_float(r["99%"]) for r in steady]),
        error_pct=round(100 * failures / requests, 2) if requests else 0.0,
    )

    window = [c for c in cluster if start <= int(c["timestamp"]) <= end]
    if window:

        def column(name: str) -> list[float]:
            return [v for v in (_float(c[name]) for c in window) if v is not None]

        def maximum(name: str) -> int | None:
            values = column(name)
            return int(max(values)) if values else None

        cpu = column("hpa_cpu_utilization")
        stage.max_ready_pods = maximum("ready_pods")
        stage.max_desired_pods = maximum("hpa_desired_replicas")
        stage.max_nodes = maximum("nodes")
        stage.max_pending_pods = maximum("pending_pods")
        stage.avg_cpu_pct = round(statistics.mean(cpu), 1) if cpu else None
        per_pod_memory = []
        for c in window:
            memory, pods = _float(c["pod_memory_mib"]), _float(c["ready_pods"])
            if memory is not None and pods:
                per_pod_memory.append(memory / pods)
        if per_pod_memory:
            stage.avg_memory_mib_per_pod = round(statistics.mean(per_pod_memory))
    return stage


def scaling_events(cluster: list[dict], test_start: int) -> list[str]:
    """Human-readable list of replica/node changes relative to test start."""
    events = []
    previous: dict[str, str] = {}
    labels = {
        "hpa_desired_replicas": "HPA desired",
        "ready_pods": "ready pods",
        "pending_pods": "pending pods",
        "nodes": "nodes",
    }
    for row in cluster:
        for key, label in labels.items():
            value = row.get(key, "")
            if value == "":
                continue
            if key in previous and previous[key] != value:
                offset = int(row["timestamp"]) - test_start
                events.append(f"t+{offset:>5}s  {label}: {previous[key]} -> {value}")
            previous[key] = value
    return events


def _fmt(value: object, suffix: str = "") -> str:
    return "-" if value is None else f"{value}{suffix}"


def render_markdown(run_dir: Path, stages: list[Stage], batch_size: int, events: list[str]) -> str:
    lines = [
        f"# Load test results: {run_dir.name}",
        "",
        f"Batch size per request: {batch_size}",
        "",
        "| Stage | Users | Duration | RPS | Images/s | P50 | P95 | P99 | Errors "
        "| Pods (max ready) | HPA desired (max) | Nodes (max) | CPU avg (% of request) "
        "| Pending (max) | Memory/pod |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stages:
        lines.append(
            f"| {s.index} | {s.users} | {s.duration_s}s | {s.rps} | {s.images_per_s} "
            f"| {_fmt(s.p50_ms, ' ms')} | {_fmt(s.p95_ms, ' ms')} | {_fmt(s.p99_ms, ' ms')} "
            f"| {s.error_pct}% | {_fmt(s.max_ready_pods)} | {_fmt(s.max_desired_pods)} "
            f"| {_fmt(s.max_nodes)} | {_fmt(s.avg_cpu_pct, '%')} | {_fmt(s.max_pending_pods)} "
            f"| {_fmt(s.avg_memory_mib_per_pod, ' MiB')} |"
        )
    if events:
        lines += ["", "## Scaling events", "", "```", *events, "```"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--min-stage", type=int, default=30, help="minimum plateau length (s)")
    args = parser.parse_args()

    history_path = args.run_dir / "locust_stats_history.csv"
    if not history_path.exists():
        print(f"{history_path} not found", file=sys.stderr)
        return 1

    rows, batch_size = read_history(history_path)
    if not rows:
        print("no Aggregated rows in stats history", file=sys.stderr)
        return 1
    cluster = read_cluster(args.run_dir / "cluster.csv")
    stages = [
        summarize_stage(i, plateau, batch_size, cluster)
        for i, plateau in enumerate(find_plateaus(rows, args.min_stage), start=1)
    ]
    events = scaling_events(cluster, int(rows[0]["Timestamp"]))

    markdown = render_markdown(args.run_dir, stages, batch_size, events)
    (args.run_dir / "summary.md").write_text(markdown)
    (args.run_dir / "summary.json").write_text(
        json.dumps(
            {"batch_size": batch_size, "stages": [asdict(s) for s in stages], "events": events},
            indent=2,
        )
        + "\n"
    )
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
