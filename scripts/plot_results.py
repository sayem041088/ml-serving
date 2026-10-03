#!/usr/bin/env python3
"""Plot a load-test run as small multiples on a shared time axis.

    scripts/plot_results.py results/<run> --output docs/images/gke-staged-scaling

Writes <output>-light.png and <output>-dark.png (GitHub serves the right one via
<picture> + prefers-color-scheme). Panels, one y-scale each: concurrent users,
successful requests/s, ready pods + nodes, and P95 latency (log scale).

Colours are the validated two-slot categorical palette (blue, orange), stepped
separately for the light and dark surfaces; text never uses series colours.
Needs matplotlib (pip install matplotlib).
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series1": "#2a78d6",
        "series2": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series1": "#3987e5",
        "series2": "#d95926",
    },
}
LINE_WIDTH = 1.6  # ~2 px at README display width
SMOOTHING = 15  # samples (~15 s) for Locust's rolling rate / percentile


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rolling(values: list[float | None], window: int, reducer) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1) : i + 1] if v is not None]
        out.append(reducer(chunk) if chunk else None)
    return out


def load(run_dir: Path, hpa_max: int | None, node_max: int | None) -> dict:
    with (run_dir / "locust_stats_history.csv").open() as f:
        rows = [r for r in csv.DictReader(f) if r["Name"] == "Aggregated"]
    rows.sort(key=lambda r: int(r["Timestamp"]))
    while rows and int(rows[-1]["User Count"]) == 0:  # Locust shutting down
        rows.pop()
    t0 = int(rows[0]["Timestamp"])
    minutes = [(int(r["Timestamp"]) - t0) / 60 for r in rows]
    ok_rps = [(_float(r["Requests/s"]) or 0.0) - (_float(r["Failures/s"]) or 0.0) for r in rows]

    data = {
        "t": minutes,
        "users": [int(r["User Count"]) for r in rows],
        "rps": _rolling(ok_rps, SMOOTHING, statistics.mean),
        "p95": _rolling([_float(r["95%"]) for r in rows], SMOOTHING, statistics.median),
        "cluster_t": [],
        "pods": [],
        "nodes": [],
        "hpa_max": hpa_max,
        "node_max": node_max,
    }
    cluster_csv = run_dir / "cluster.csv"
    if cluster_csv.exists():
        with cluster_csv.open() as f:
            for r in csv.DictReader(f):
                if r["ready_pods"] and r["nodes"]:
                    data["cluster_t"].append((int(r["timestamp"]) - t0) / 60)
                    data["pods"].append(int(r["ready_pods"]))
                    data["nodes"].append(int(r["nodes"]))
    return data


def stage_starts(t: list[float], users: list[int]) -> list[tuple[float, int]]:
    """(start minute, users) for each plateau, ignoring short ramps."""
    starts: list[tuple[float, int]] = []
    for minute, count in zip(t, users, strict=True):
        if not starts or count != starts[-1][1]:
            starts.append((minute, count))
    return [
        (m, u)
        for i, (m, u) in enumerate(starts)
        if i + 1 == len(starts) or starts[i + 1][0] - m > 0.5
    ]


def _style_axis(ax, theme: dict, title: str) -> None:
    ax.set_facecolor(theme["surface"])
    ax.set_title(title, loc="left", fontsize=11, color=theme["primary"], pad=6)
    ax.grid(axis="y", color=theme["grid"], linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(theme["axis"])
    ax.tick_params(colors=theme["muted"], labelsize=9, length=0)


def plot(data: dict, theme_name: str, output: Path, title: str, subtitle: str) -> Path:
    theme = THEMES[theme_name]
    fig, axes = plt.subplots(
        4, 1, figsize=(10, 10), sharex=True, gridspec_kw={"height_ratios": [1, 1, 1.25, 1.25]}
    )
    fig.patch.set_facecolor(theme["surface"])
    stages = stage_starts(data["t"], data["users"])
    line = {"linewidth": LINE_WIDTH, "solid_capstyle": "round", "solid_joinstyle": "round"}

    def stage_lines(ax) -> None:
        for minute, _ in stages[1:]:
            ax.axvline(minute, color=theme["grid"], linewidth=0.8, zorder=0)

    def reference(ax, y: float, label: str) -> None:
        # Dashed only for thresholds, so they never read as gridlines.
        ax.axhline(y, color=theme["muted"], linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.text(
            data["t"][-1],
            y,
            f"  {label}",
            va="center",
            ha="left",
            fontsize=9,
            color=theme["secondary"],
        )

    users_ax, rps_ax, cap_ax, lat_ax = axes

    _style_axis(users_ax, theme, "Offered load: concurrent users")
    stage_lines(users_ax)
    users_ax.step(data["t"], data["users"], where="post", color=theme["series1"], **line)
    users_ax.set_ylim(0, max(data["users"]) * 1.25)
    for minute, users in stages:
        users_ax.text(minute + 0.15, users + max(data["users"]) * 0.04, f"{users}",
                      fontsize=9, color=theme["secondary"])  # fmt: skip

    _style_axis(rps_ax, theme, "Throughput: successful predictions per second")
    stage_lines(rps_ax)
    rps_ax.plot(data["t"], data["rps"], color=theme["series1"], **line)
    # Label the saturation plateau (the stage with the most users), not a
    # momentary spike: the plateau is the capacity ceiling.
    top_users = max(data["users"])
    plateau = [
        (t, v)
        for t, u, v in zip(data["t"], data["users"], data["rps"], strict=True)
        if u == top_users and v is not None
    ]
    ceiling = statistics.median(v for _, v in plateau)
    mid_t = plateau[len(plateau) // 2][0]
    rps_ax.set_ylim(0, max(v for v in data["rps"] if v is not None) * 1.3)
    rps_ax.annotate(f"plateau ≈ {ceiling:.0f}/s", (mid_t, ceiling), xytext=(0, 10),
                    textcoords="offset points", ha="center", fontsize=9,
                    color=theme["secondary"])  # fmt: skip

    _style_axis(cap_ax, theme, "Capacity: ready serving pods (HPA) and nodes (cluster autoscaler)")
    stage_lines(cap_ax)
    if data["pods"]:
        cap_ax.step(data["cluster_t"], data["pods"], where="post", color=theme["series1"],
                    label="ready pods", **line)  # fmt: skip
        cap_ax.step(data["cluster_t"], data["nodes"], where="post", color=theme["series2"],
                    label="nodes", **line)  # fmt: skip
        top = max([*data["pods"], data["hpa_max"] or 0]) + 2
        cap_ax.set_ylim(0, top)
        if data["hpa_max"]:
            reference(cap_ax, data["hpa_max"], f"HPA max {data['hpa_max']}")
        if data["node_max"]:
            reference(cap_ax, data["node_max"], f"node pool max {data['node_max']}")
        legend = cap_ax.legend(loc="upper left", frameon=False, fontsize=9, ncols=2)
        for text in legend.get_texts():
            text.set_color(theme["secondary"])

    _style_axis(lat_ax, theme, "Latency: P95, client side (log scale)")
    stage_lines(lat_ax)
    lat_ax.plot(data["t"], data["p95"], color=theme["series1"], **line)
    lat_ax.set_yscale("log")
    ticks = [200, 500, 1000, 2000, 5000, 10000, 30000]
    lat_ax.set_yticks(ticks, ["200 ms", "500 ms", "1 s", "2 s", "5 s", "10 s", "30 s"])
    lat_ax.minorticks_off()
    lat_ax.set_ylim(150, 40000)
    reference(lat_ax, 500, "SLO 500 ms")
    lat_ax.set_xlabel("minutes into the test", color=theme["muted"], fontsize=9)
    lat_ax.set_xlim(0, data["t"][-1])

    fig.suptitle(title, x=0.06, y=0.985, ha="left", fontsize=14, color=theme["primary"])
    fig.text(0.06, 0.955, subtitle, ha="left", fontsize=9.5, color=theme["secondary"])
    fig.subplots_adjust(left=0.08, right=0.86, top=0.91, bottom=0.06, hspace=0.42)

    path = output.with_name(f"{output.name}-{theme_name}.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160, facecolor=theme["surface"])
    plt.close(fig)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="path prefix, no extension")
    parser.add_argument("--hpa-max", type=int, default=10)
    parser.add_argument("--node-max", type=int, default=5)
    parser.add_argument("--title", default="Staged load test")
    parser.add_argument("--subtitle", default="")
    args = parser.parse_args()

    data = load(args.run_dir, args.hpa_max, args.node_max)
    for theme in THEMES:
        print(plot(data, theme, args.output, args.title, args.subtitle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
