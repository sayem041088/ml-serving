"""Locust profiles and the results summariser."""

from __future__ import annotations

import csv
from pathlib import Path

import summarize_results

from profiles import PROFILES

HISTORY_FIELDS = [
    "Timestamp",
    "User Count",
    "Type",
    "Name",
    "Requests/s",
    "Failures/s",
    "50%",
    "95%",
    "99%",
    "Total Request Count",
    "Total Failure Count",
]


def test_profiles_are_well_formed():
    for name, stages in PROFILES.items():
        assert stages, name
        for duration, users, spawn_rate in stages:
            assert duration > 0 and users > 0 and spawn_rate > 0, name


def test_staged_profile_follows_the_plan():
    assert [users for _, users, _ in PROFILES["staged"]] == [10, 50, 100, 300, 500, 50]


def _write_history(path: Path, stages: list[tuple[int, int, float, int]]) -> None:
    """stages: (users, seconds, rps, failures_per_second); one row per second."""
    t, total, failed = 1_000, 0, 0
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for users, seconds, rps, fps in stages:
            for _ in range(seconds):
                total += int(rps)
                failed += fps
                row = {
                    "Timestamp": t,
                    "User Count": users,
                    "Type": "",
                    "Name": "Aggregated",
                    "Requests/s": rps,
                    "Failures/s": fps,
                    "50%": 100 * users,
                    "95%": 200 * users,
                    "99%": "N/A",
                    "Total Request Count": total,
                    "Total Failure Count": failed,
                }
                writer.writerow(row)
                writer.writerow({**row, "Type": "POST", "Name": "predict batch=16"})
                t += 1


def test_summarize_detects_stages_and_joins_cluster_metrics(tmp_path):
    # Stage 1: 10 users, 60 s, 10 req/s; ramp (5 s, ignored); stage 2: 50 users, 60 s, 20 req/s
    # with 1 failure/s.
    _write_history(
        tmp_path / "locust_stats_history.csv",
        [(10, 60, 10, 0), (30, 5, 15, 0), (50, 60, 20, 1)],
    )
    with (tmp_path / "cluster.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["timestamp", "hpa_current_replicas", "hpa_desired_replicas", "hpa_cpu_utilization",
             "ready_pods", "pending_pods", "nodes", "ready_nodes", "pod_cpu_millicores",
             "pod_memory_mib"]
        )  # fmt: skip
        writer.writerow([1_010, 2, 2, 40, 2, 0, 2, 2, 800, 1400])
        writer.writerow([1_100, 2, 4, 90, 2, 2, 2, 2, 1800, 1400])
        writer.writerow([1_120, 4, 4, 70, 4, 0, 3, 3, 2800, 2800])

    rows, batch = summarize_results.read_history(tmp_path / "locust_stats_history.csv")
    assert batch == 16
    plateaus = summarize_results.find_plateaus(rows, min_stage_s=30)
    assert [p[0]["User Count"] for p in plateaus] == ["10", "50"]

    cluster = summarize_results.read_cluster(tmp_path / "cluster.csv")
    first, second = (
        summarize_results.summarize_stage(i, p, batch, cluster) for i, p in enumerate(plateaus, 1)
    )
    assert first.rps == 10.0 and first.images_per_s == 160.0 and first.error_pct == 0.0
    assert first.p95_ms == 2000 and first.p99_ms is None
    assert first.max_ready_pods == 2 and first.avg_cpu_pct == 40.0
    assert second.rps == 20.0 and second.error_pct == 5.0
    assert second.max_ready_pods == 4 and second.max_nodes == 3 and second.max_pending_pods == 2

    events = summarize_results.scaling_events(cluster, test_start=1_000)
    assert "t+  100s  HPA desired: 2 -> 4" in events
    assert "t+  120s  nodes: 2 -> 3" in events
