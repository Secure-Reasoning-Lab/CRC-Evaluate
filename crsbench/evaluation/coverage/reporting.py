"""Reporting helpers for coverage timeline analysis."""

from __future__ import annotations

import csv
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from crsbench.evaluation.coverage.models import CoverageTimelineReport


def write_timeline_json(report: CoverageTimelineReport, output_path: Path) -> None:
    """Write a coverage timeline report as JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))


def write_timeline_csv(report: CoverageTimelineReport, output_path: Path) -> None:
    """Write the per-seed coverage timeline as CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "relative_time",
                "content_hash",
                "original_name",
                "size",
                "lines_covered",
                "crashed",
                "raw_cov_path",
                "crash_log_path",
            ]
        )
        for seed in sorted(report.seeds, key=lambda item: item.relative_time):
            writer.writerow(
                [
                    seed.relative_time,
                    seed.content_hash,
                    seed.original_name,
                    seed.size,
                    seed.lines_covered,
                    seed.crashed,
                    str(seed.raw_cov_path) if seed.raw_cov_path is not None else "",
                    (
                        str(seed.crash_log_path)
                        if seed.crash_log_path is not None
                        else ""
                    ),
                ]
            )


def write_timeline_png(report: CoverageTimelineReport, output_path: Path) -> None:
    """Render a PNG covered-lines-over-time graph from per-seed replay."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    seeds = sorted(report.seeds, key=lambda item: item.relative_time)
    if seeds:
        x_values = [seed.relative_time for seed in seeds]
        y_values = [seed.lines_covered for seed in seeds]
        ax.step(x_values, y_values, where="post", label="Covered lines", linewidth=2)
        min_x = min(x_values)
        if report.pov_markers:
            min_x = min(
                min_x, min(marker.relative_time for marker in report.pov_markers)
            )
        ax.set_xlim(left=min(0.0, min_x))
    if report.pov_markers:
        marker_y = max([seed.lines_covered for seed in seeds] or [0.0])
        for marker in report.pov_markers:
            ax.axvline(
                marker.relative_time,
                color="tab:red",
                alpha=0.35,
                linestyle="--",
                linewidth=1,
            )
            ax.scatter(
                [marker.relative_time],
                [marker_y],
                color="tab:red",
                s=14,
                zorder=3,
            )

    ax.set_title(f"{report.benchmark} / {report.harness} covered lines")
    ax.set_xlabel("Relative time (seconds)")
    ax.set_ylabel("Covered lines")
    ax.set_ylim(
        0,
        max(1.0, max((seed.lines_covered for seed in seeds), default=0.0)),
    )
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
