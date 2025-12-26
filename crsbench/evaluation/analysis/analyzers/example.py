"""Example CRS analyzer - reference implementation.

This is a minimal working example that CRS developers can copy and adapt
for their own CRS implementations.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from crsbench.evaluation.analysis.base import AnalysisResult, AnalyzerInterface
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class ExampleAnalyzer(AnalyzerInterface):
    """Minimal reference implementation showing the analyzer pattern.

    This is a working example that CRS developers can copy and adapt.
    It assumes crs-data contains a simple metrics.json file.

    Expected crs-data/ structure:
        crs-data/
        └── metrics.json  # JSON file with metrics

    Example metrics.json:
        {
          "iterations": 100,
          "discoveries": 5,
          "timestamp": 1234567890.0
        }
    """

    @property
    def crs_name(self) -> str:
        """Return CRS name for this analyzer."""
        return "example-crs"

    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        """Analyze example CRS snapshot data.

        Args:
            snapshot_dir: Extracted snapshot directory containing crs-data/

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid
        """
        crs_data_dir = snapshot_dir / "crs-data"
        if not crs_data_dir.exists():
            logger.debug(f"No crs-data directory in {snapshot_dir}")
            return None

        metrics_file = crs_data_dir / "metrics.json"
        if not metrics_file.exists():
            # Return result with warning instead of None to demonstrate this pattern
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="No metrics available",
                warnings=["metrics.json not found in crs-data/"],
                metadata={"analyzer_version": "1.0"},
            )

        try:
            with metrics_file.open() as f:
                data = json.load(f)

            # Extract whatever metrics your CRS writes
            metrics = {
                "iterations": data.get("iterations", 0),
                "discoveries": data.get("discoveries", 0),
            }

            # Add timestamp if available
            if "timestamp" in data:
                metrics["timestamp"] = data["timestamp"]

            summary = (
                f"Example CRS: {metrics['iterations']} iterations, "
                f"{metrics['discoveries']} discoveries"
            )

            return AnalysisResult(
                crs_name=self.crs_name,
                metrics=metrics,
                summary=summary,
                warnings=[],
                metadata={"analyzer_version": "1.0"},
            )

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in {metrics_file}: {e}")
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Analysis failed",
                warnings=[f"Invalid JSON in metrics.json: {e}"],
                metadata={"analyzer_version": "1.0"},
            )

        except Exception as e:
            logger.warning(f"Error reading metrics from {metrics_file}: {e}")
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Analysis failed",
                warnings=[f"Error reading metrics: {e}"],
                metadata={"analyzer_version": "1.0"},
            )

    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data.

        Args:
            trial_dir: Trial output directory

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid
        """
        crs_data_dir = trial_dir / "output" / "crs-data"
        if not crs_data_dir.exists():
            logger.debug(f"No output/crs-data directory in {trial_dir}")
            return None

        # Reuse snapshot logic by creating a "snapshot-like" view
        # The snapshot logic expects snapshot_dir/crs-data/
        # We have trial_dir/output/crs-data/
        # So we pass trial_dir/output as the "snapshot_dir"
        temp_snapshot_view = trial_dir / "output"
        return self.analyze_snapshot(temp_snapshot_view)

    def analyze_time_series(self, snapshots: List[Path]) -> Optional[Dict[str, Any]]:
        """Analyze trends across snapshots.

        Args:
            snapshots: List of extracted snapshot directories (sorted by cycle)

        Returns:
            Dict with time-series metrics, None if insufficient data
        """
        iterations_over_time = []
        discoveries_over_time = []
        timestamps = []

        for snapshot_dir in sorted(snapshots):
            result = self.analyze_snapshot(snapshot_dir)
            if result and result.metrics:
                iterations_over_time.append(result.metrics.get("iterations", 0))
                discoveries_over_time.append(result.metrics.get("discoveries", 0))
                if "timestamp" in result.metrics:
                    timestamps.append(result.metrics["timestamp"])

        if not iterations_over_time:
            logger.debug("No time-series data available")
            return None

        time_series_data = {
            "iterations_over_time": iterations_over_time,
            "discoveries_over_time": discoveries_over_time,
            "avg_iterations_per_cycle": sum(iterations_over_time)
            / len(iterations_over_time),
            "total_discoveries": sum(discoveries_over_time),
        }

        if timestamps:
            time_series_data["timestamps"] = timestamps

        return time_series_data
