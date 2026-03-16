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
    """Write the bucketed coverage curve as CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "bucket_start",
                "bucket_end",
                "inputs_seen",
                "lines_covered",
                "lines_total",
                "lines_percent",
            ]
        )
        for bucket in report.buckets:
            writer.writerow(
                [
                    bucket.bucket_start,
                    bucket.bucket_end,
                    bucket.inputs_seen,
                    bucket.lines_covered,
                    bucket.lines_total,
                    bucket.lines_percent,
                ]
            )


def write_timeline_png(report: CoverageTimelineReport, output_path: Path) -> None:
    """Render a PNG line-coverage-over-time graph with POV markers."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    if report.buckets:
        x_values = [bucket.bucket_end for bucket in report.buckets]
        y_values = [bucket.lines_percent for bucket in report.buckets]
        ax.step(x_values, y_values, where="post", label="Line coverage", linewidth=2)
        ax.set_xlim(left=0)
    if report.pov_markers:
        marker_y = max([bucket.lines_percent for bucket in report.buckets] or [0.0])
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

    ax.set_title(f"{report.benchmark} / {report.harness} line coverage")
    ax.set_xlabel("Relative time (seconds)")
    ax.set_ylabel("Line coverage (%)")
    ax.set_ylim(
        0,
        max(
            100.0, max((bucket.lines_percent for bucket in report.buckets), default=0.0)
        ),
    )
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
