"""CSV report generator for CRSBench experiment results."""

import csv
from pathlib import Path
from typing import Any

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class CSVReportGenerator:
    """Generate CSV reports from experiment metrics."""

    def __init__(self, output_dir: Path) -> None:
        """Initialize CSV report generator.

        Args:
            output_dir: Directory to write CSV reports
        """
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_trial_report(
        self, trial_metrics: dict[str, Any], _snapshots: list[Any] | None = None
    ) -> list[Path]:
        """Generate CSV report for a single trial.

        Args:
            trial_metrics: Trial-level metrics with time_series data
            _snapshots: Unused (kept for API compatibility)

        Returns:
            List of generated CSV file paths
        """
        output_files = []

        # Create trial-reports subdirectory (consistent with JSON/HTML)
        trial_reports_dir = self.output_dir / "trial-reports"
        trial_reports_dir.mkdir(exist_ok=True)

        # Create unique filename prefix from trial path
        # e.g., "exp/afc-curl/curl_fuzzer/delta/trial-1" -> "afc-curl-curl_fuzzer-delta-trial-1"
        trial_path = (
            Path(trial_metrics["trial_dir"])
            if isinstance(trial_metrics, dict)
            else Path(trial_metrics.trial_dir)
        )
        # Skip experiment dir (first part) and join the rest
        trial_id = (
            "-".join(trial_path.parts[1:])
            if len(trial_path.parts) > 1
            else trial_path.name
        )

        # Generate trial summary CSV
        trial_csv = trial_reports_dir / f"{trial_id}_summary.csv"
        trial_row = self._format_trial_row(trial_metrics)
        self._write_csv_rows(trial_csv, [trial_row], list(trial_row.keys()))
        output_files.append(trial_csv)
        logger.info(f"Generated trial summary CSV: {trial_csv}")

        # Generate time series CSV
        # Use time_series (computed cumulative data) not raw snapshots
        time_series_data = (
            trial_metrics.get("time_series", [])
            if isinstance(trial_metrics, dict)
            else trial_metrics.time_series
        )
        if time_series_data:
            time_series_csv = trial_reports_dir / f"{trial_id}_time_series.csv"
            time_series_rows = [
                self._format_time_series_row(trial_metrics, ts_point)
                for ts_point in time_series_data
            ]
            if time_series_rows:
                self._write_csv_rows(
                    time_series_csv, time_series_rows, list(time_series_rows[0].keys())
                )
                output_files.append(time_series_csv)
                logger.info(f"Generated time series CSV: {time_series_csv}")

        return output_files

    def generate_experiment_report(
        self, experiment_metrics: dict[str, Any]
    ) -> list[Path]:
        """Generate CSV reports for entire experiment.

        Args:
            experiment_metrics: Experiment-level aggregated metrics

        Returns:
            List of generated CSV file paths
        """
        output_files = []

        # Generate trial summary CSV
        trial_csv = self.output_dir / "trial_summary.csv"
        trial_rows = [
            self._format_trial_row(trial)
            for trial in experiment_metrics["trial_metrics"]
        ]
        if trial_rows:
            self._write_csv_rows(trial_csv, trial_rows, list(trial_rows[0].keys()))
            output_files.append(trial_csv)
            logger.info(f"Generated trial summary CSV: {trial_csv}")

        # Generate CRS summary CSV
        crs_csv = self.output_dir / "crs_summary.csv"
        crs_rows = [
            self._format_crs_row(crs_name, crs_data)
            for crs_name, crs_data in experiment_metrics.get("by_crs", {}).items()
        ]
        if crs_rows:
            self._write_csv_rows(crs_csv, crs_rows, list(crs_rows[0].keys()))
            output_files.append(crs_csv)
            logger.info(f"Generated CRS summary CSV: {crs_csv}")

        # Generate benchmark summary CSV
        benchmark_csv = self.output_dir / "benchmark_summary.csv"
        benchmark_rows = [
            self._format_benchmark_row(bench_name, bench_data)
            for bench_name, bench_data in experiment_metrics.get(
                "by_benchmark", {}
            ).items()
        ]
        if benchmark_rows:
            self._write_csv_rows(
                benchmark_csv, benchmark_rows, list(benchmark_rows[0].keys())
            )
            output_files.append(benchmark_csv)
            logger.info(f"Generated benchmark summary CSV: {benchmark_csv}")

        # Generate time series CSV (all trials)
        time_series_csv = self.output_dir / "time_series.csv"
        time_series_rows = []
        for trial in experiment_metrics["trial_metrics"]:
            # Use time_series (computed cumulative data) not raw snapshots
            for ts_point in trial.get("time_series", []):
                time_series_rows.append(self._format_time_series_row(trial, ts_point))
        if time_series_rows:
            self._write_csv_rows(
                time_series_csv, time_series_rows, list(time_series_rows[0].keys())
            )
            output_files.append(time_series_csv)
            logger.info(f"Generated time series CSV: {time_series_csv}")

        # Generate combined report CSV
        combined_csv = self.output_dir / "combined_report.csv"
        combined_rows = self._create_combined_rows(
            trial_rows, crs_rows, benchmark_rows, time_series_rows
        )
        if combined_rows:
            # Get all unique columns across all record types
            all_columns = set()
            for row in combined_rows:
                all_columns.update(row.keys())
            # Sort columns with record_type first
            sorted_columns = ["record_type"] + sorted(
                col for col in all_columns if col != "record_type"
            )
            self._write_csv_rows(combined_csv, combined_rows, sorted_columns)
            output_files.append(combined_csv)
            logger.info(f"Generated combined report CSV: {combined_csv}")

        return output_files

    def _format_trial_row(self, trial_metrics: dict[str, Any]) -> dict[str, Any]:
        """Format trial metrics into CSV row.

        Args:
            trial_metrics: Trial metrics dictionary

        Returns:
            Dictionary representing CSV row
        """
        return {
            "trial_num": trial_metrics.get("trial_num", ""),
            "crs": trial_metrics.get("crs", ""),
            "benchmark": trial_metrics.get("benchmark", ""),
            "harness": trial_metrics.get("harness", ""),
            "mode": trial_metrics.get("mode", ""),
            "run_mode": trial_metrics.get("run_mode", ""),
            "sanitizer": trial_metrics.get("sanitizer", ""),
            "total_povs": trial_metrics.get("total_povs", 0),
            "unique_povs": trial_metrics.get("unique_povs", 0),
            "total_patches": trial_metrics.get("total_patches", 0),
            "unique_patches": trial_metrics.get("unique_patches", 0),
            "total_llm_cost": trial_metrics.get("total_llm_cost", 0.0),
            "total_llm_tokens": trial_metrics.get("total_llm_tokens", 0),
            "total_llm_input_tokens": trial_metrics.get("total_llm_input_tokens", 0),
            "total_llm_output_tokens": trial_metrics.get("total_llm_output_tokens", 0),
            "total_time": trial_metrics.get("total_time", 0.0),
            "time_to_first_pov": trial_metrics.get("time_to_first_pov", ""),
            "snapshot_count": trial_metrics.get("snapshot_count", 0),
        }

    def _format_crs_row(
        self, crs_name: str, crs_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Format CRS summary into CSV row.

        Args:
            crs_name: CRS name
            crs_data: CRS aggregated data

        Returns:
            Dictionary representing CSV row
        """
        return {
            "crs": crs_name,
            "trial_count": crs_data.get("trial_count", 0),
            "avg_povs": crs_data.get("avg_povs", 0.0),
            "avg_patches": crs_data.get("avg_patches", 0.0),
            "avg_cost": crs_data.get("avg_cost", 0.0),
            "total_cost": crs_data.get("total_cost", 0.0),
            "total_povs": crs_data.get("total_povs", 0),
        }

    def _format_benchmark_row(
        self, bench_name: str, bench_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Format benchmark summary into CSV row.

        Args:
            bench_name: Benchmark name
            bench_data: Benchmark aggregated data

        Returns:
            Dictionary representing CSV row
        """
        return {
            "benchmark": bench_name,
            "trial_count": bench_data.get("trial_count", 0),
            "avg_povs": bench_data.get("avg_povs", 0.0),
            "avg_patches": bench_data.get("avg_patches", 0.0),
            "avg_time_to_first_pov": bench_data.get("avg_time_to_first_pov", 0.0),
            "total_cost": bench_data.get("total_cost", 0.0),
        }

    def _format_time_series_row(
        self, trial_metrics: dict[str, Any], ts_point: dict[str, Any]
    ) -> dict[str, Any]:
        """Format time series point into CSV row.

        Args:
            trial_metrics: Trial-level metrics
            ts_point: TimeSeriesPoint data dict

        Returns:
            Dictionary representing CSV row
        """
        return {
            "trial_num": trial_metrics.get("trial_num", ""),
            "crs": trial_metrics.get("crs", ""),
            "benchmark": trial_metrics.get("benchmark", ""),
            "harness": trial_metrics.get("harness", ""),
            "mode": trial_metrics.get("mode", ""),
            "run_mode": trial_metrics.get("run_mode", ""),
            "sanitizer": trial_metrics.get("sanitizer", ""),
            "elapsed_time": ts_point.get("elapsed_time", 0.0),
            "running_elapsed_time": ts_point.get("running_elapsed_time", 0.0),
            "cumulative_povs": ts_point.get("cumulative_povs", 0),
            "cumulative_patches": ts_point.get("cumulative_patches", 0),
            "llm_tokens": ts_point.get("llm_tokens", 0),
            "llm_input_tokens": ts_point.get("llm_input_tokens", 0),
            "llm_output_tokens": ts_point.get("llm_output_tokens", 0),
            "llm_cost": ts_point.get("llm_cost", 0.0),
        }

    def _create_combined_rows(
        self,
        trial_rows: list[dict[str, Any]],
        crs_rows: list[dict[str, Any]],
        benchmark_rows: list[dict[str, Any]],
        time_series_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Create combined report with record_type column.

        Args:
            trial_rows: Trial summary rows
            crs_rows: CRS summary rows
            benchmark_rows: Benchmark summary rows
            time_series_rows: Time series rows

        Returns:
            Combined list of rows with record_type field
        """
        combined = []

        for row in trial_rows:
            combined.append({"record_type": "trial", **row})

        for row in crs_rows:
            combined.append({"record_type": "crs", **row})

        for row in benchmark_rows:
            combined.append({"record_type": "benchmark", **row})

        for row in time_series_rows:
            combined.append({"record_type": "time_series", **row})

        return combined

    def _write_csv_rows(
        self, filepath: Path, rows: list[dict[str, Any]], fieldnames: list[str]
    ) -> None:
        """Write rows to CSV file.

        Args:
            filepath: Path to CSV file
            rows: List of row dictionaries
            fieldnames: List of column names
        """
        with filepath.open("w", newline="") as csvfile:
            writer = csv.DictWriter(
                csvfile, fieldnames=fieldnames, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
