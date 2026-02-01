"""JSON report generation for the reporting module."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crsbench.reporting.models import (
    ExperimentMetrics,
    SnapshotData,
    TrialMetrics,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class JSONReportGenerator:
    """Generate JSON reports from metrics and snapshot data.

    JSON reports are structured for programmatic analysis and
    can be easily parsed by other tools.

    Example:
        generator = JSONReportGenerator(output_dir=Path("./reports"))
        report_path = generator.generate_trial_report(trial_metrics, snapshots)
    """

    def __init__(self, output_dir: Path):
        """Initialize the JSON report generator.

        Args:
            output_dir: Directory to write reports to
        """
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_trial_report(
        self,
        trial_metrics: TrialMetrics,
        snapshots: list[SnapshotData],
    ) -> Path:
        """Generate JSON report for a single trial.

        Args:
            trial_metrics: Aggregated metrics for the trial
            snapshots: List of snapshot data

        Returns:
            Path to generated report file
        """
        # Build timeline from snapshots
        timeline = self._build_timeline(snapshots)

        report: dict[str, Any] = {
            "report_type": "trial",
            "generated_at": datetime.now().isoformat(),
            "trial": {
                "trial_dir": trial_metrics.trial_dir,
                "trial_num": trial_metrics.trial_num,
                "crs": trial_metrics.crs,
                "benchmark": trial_metrics.benchmark,
                "harness": trial_metrics.harness,
                "mode": trial_metrics.mode,
            },
            "summary": {
                "total_povs_discovered": trial_metrics.total_povs_discovered,
                "unique_povs": trial_metrics.unique_povs,
                "total_patches_generated": trial_metrics.total_patches_generated,
                "unique_patches": trial_metrics.unique_patches,
                "total_llm_cost": trial_metrics.total_llm_cost,
                "total_llm_tokens": trial_metrics.total_llm_tokens,
                "total_time": trial_metrics.total_time,
                "time_to_first_pov": trial_metrics.time_to_first_pov,
                "snapshot_count": trial_metrics.snapshot_count,
            },
            "povs": {
                "unique_names": trial_metrics.unique_pov_names,
                "count": trial_metrics.unique_povs,
            },
            "patches": {
                "unique_names": trial_metrics.unique_patch_names,
                "count": trial_metrics.unique_patches,
            },
            "llm_usage": {
                "total_tokens": trial_metrics.total_llm_tokens,
                "total_cost": trial_metrics.total_llm_cost,
                "by_model": trial_metrics.llm_usage_by_model,
            },
            "time_series": [
                {
                    "elapsed_time": p.elapsed_time,
                    "cumulative_povs": p.cumulative_povs,
                    "cumulative_patches": p.cumulative_patches,
                    "llm_tokens": p.llm_tokens,
                    "llm_cost": p.llm_cost,
                }
                for p in trial_metrics.time_series
            ],
            "timeline": timeline,
        }

        # Write report
        trial_reports_dir = self.output_dir / "trial-reports"
        trial_reports_dir.mkdir(exist_ok=True)

        # Create unique filename from trial path
        # e.g., "exp/afc-curl/curl_fuzzer/delta/trial-1" -> "afc-curl-curl_fuzzer-delta-trial-1"
        trial_path = Path(trial_metrics.trial_dir)
        # Skip experiment dir (first part) and join the rest
        trial_id = (
            "-".join(trial_path.parts[1:])
            if len(trial_path.parts) > 1
            else trial_path.name
        )
        output_path = trial_reports_dir / f"{trial_id}.json"
        output_path.write_text(json.dumps(report, indent=2))

        logger.info(f"Generated JSON trial report: {output_path}")
        return output_path

    def generate_experiment_report(
        self,
        experiment_metrics: ExperimentMetrics,
        trial_report_paths: list[Path] | None = None,
    ) -> Path:
        """Generate JSON report for an entire experiment.

        Args:
            experiment_metrics: Aggregated experiment metrics
            trial_report_paths: Paths to individual trial reports (optional)

        Returns:
            Path to generated report file
        """
        # Convert CRS metrics to dict
        by_crs = {crs: m.model_dump() for crs, m in experiment_metrics.by_crs.items()}

        # Convert benchmark metrics to dict
        by_benchmark = {
            benchmark: m.model_dump()
            for benchmark, m in experiment_metrics.by_benchmark.items()
        }

        report: dict[str, Any] = {
            "report_type": "experiment",
            "generated_at": datetime.now().isoformat(),
            "experiment_dir": experiment_metrics.experiment_dir,
            "summary": {
                "total_trials": experiment_metrics.total_trials,
                "valid_trials": experiment_metrics.valid_trials,
                "avg_povs_per_trial": experiment_metrics.avg_povs_per_trial,
                "avg_patches_per_trial": experiment_metrics.avg_patches_per_trial,
                "avg_cost_per_trial": experiment_metrics.avg_cost_per_trial,
            },
            "by_crs": by_crs,
            "by_benchmark": by_benchmark,
            "trial_summaries": [
                {
                    "trial_dir": m.trial_dir,
                    "trial_num": m.trial_num,
                    "crs": m.crs,
                    "benchmark": m.benchmark,
                    "harness": m.harness,
                    "mode": m.mode,
                    "total_povs": m.total_povs_discovered,
                    "unique_povs": m.unique_povs,
                    "total_patches": m.total_patches_generated,
                    "unique_patches": m.unique_patches,
                    "total_cost": m.total_llm_cost,
                    "total_time": m.total_time,
                    "time_to_first_pov": m.time_to_first_pov,
                }
                for m in experiment_metrics.trial_metrics
            ],
        }

        if trial_report_paths:
            report["trial_report_files"] = [str(p) for p in trial_report_paths]

        # Extract experiment name from dir
        exp_name = Path(experiment_metrics.experiment_dir).name
        output_path = self.output_dir / f"experiment-{exp_name}.json"
        output_path.write_text(json.dumps(report, indent=2))

        logger.info(f"Generated JSON experiment report: {output_path}")
        return output_path

    def _build_timeline(self, snapshots: list[SnapshotData]) -> dict[str, Any]:
        """Build timeline data from snapshots.

        Args:
            snapshots: List of snapshot data

        Returns:
            Timeline data dictionary
        """
        sorted_snapshots = sorted(snapshots, key=lambda s: s.cycle)

        cumulative_povs = 0
        cumulative_patches = 0
        snapshot_entries = []

        for snapshot in sorted_snapshots:
            cumulative_povs += snapshot.pov_count
            cumulative_patches += snapshot.patch_count

            snapshot_entries.append(
                {
                    "cycle": snapshot.cycle,
                    "timestamp": snapshot.timestamp,
                    "elapsed_time": snapshot.elapsed_time,
                    "povs_in_snapshot": snapshot.pov_count,
                    "cumulative_povs": cumulative_povs,
                    "patches_in_snapshot": snapshot.patch_count,
                    "cumulative_patches": cumulative_patches,
                    "llm_cost": snapshot.llm_usage.total_cost_usd,
                    "llm_tokens": (
                        snapshot.llm_usage.total_input_tokens
                        + snapshot.llm_usage.total_output_tokens
                    ),
                }
            )

        return {
            "total_snapshots": len(sorted_snapshots),
            "snapshots": snapshot_entries,
        }
