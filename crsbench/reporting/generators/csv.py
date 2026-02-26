"""CSV report generator for CRSBench experiment results."""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from crsbench.evaluation.trial_paths import TrialDir
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
        logger.debug(f"Generated trial summary CSV: {trial_csv}")

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
                logger.debug(f"Generated time series CSV: {time_series_csv}")

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
            logger.debug(f"Generated trial summary CSV: {trial_csv}")

        # Generate CRS summary CSV
        crs_csv = self.output_dir / "crs_summary.csv"
        crs_rows = [
            self._format_crs_row(crs_name, crs_data)
            for crs_name, crs_data in experiment_metrics.get("by_crs", {}).items()
        ]
        if crs_rows:
            self._write_csv_rows(crs_csv, crs_rows, list(crs_rows[0].keys()))
            output_files.append(crs_csv)
            logger.debug(f"Generated CRS summary CSV: {crs_csv}")

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
            logger.debug(f"Generated benchmark summary CSV: {benchmark_csv}")

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
            logger.debug(f"Generated time series CSV: {time_series_csv}")

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
            logger.debug(f"Generated combined report CSV: {combined_csv}")

        return output_files

    def generate_patch_analysis_report(self, experiment_dir: Path) -> Path:
        """Generate patch analysis CSV from per-trial artifacts.

        Columns:
            crs, benchmark, harness, cpv, mode, sanitizer, status,
            patch_generated_count, verified_valid_count, verified_total_count,
            no_crash_all_verified, unittest_all_verified,
            verification_build_failed, verification_pov_still_triggers,
            verification_test_failed, verification_error,
            build_time, run_time, verify_time,
            elapsed_seconds, llm_cost_usd, llm_requests, trial_dir
        """
        out_path = self.output_dir / "patch_analysis.csv"
        rows: list[dict[str, Any]] = []

        for trial_dir in sorted(experiment_dir.rglob("trial-*")):
            if not trial_dir.is_dir():
                continue
            paths = TrialDir(trial_dir)

            metadata_path = paths.metadata_path
            if not metadata_path.exists():
                continue

            try:
                metadata = json.loads(metadata_path.read_text())
            except Exception:
                continue

            status = self._resolve_trial_status(trial_dir)
            cpv_id = metadata.get("target_cpv_id")
            run_mode = metadata.get("build_mode")
            sanitizer = metadata.get("sanitizer")

            patch_verif_path = paths.patch_verification_results_path
            patch_data: dict[str, Any] = {}
            if patch_verif_path.exists():
                try:
                    patch_data = json.loads(patch_verif_path.read_text())
                except Exception:
                    patch_data = {}

            summary = (
                patch_data.get("summary", {}) if isinstance(patch_data, dict) else {}
            )
            results = (
                patch_data.get("results", []) if isinstance(patch_data, dict) else []
            )
            if not isinstance(results, list):
                results = []

            discovered_patch_files = paths.count_visible_patch_diffs()
            patch_generated = max(
                int(summary.get("patches_generated", 0) or 0),
                len(results),
                discovered_patch_files,
            )

            verified_valid = int(summary.get("valid", 0) or 0)
            verified_total = len(results)
            no_crash_all_verified = bool(
                verified_total > 0
                and all(r.get("pov_test_passed") is True for r in results)
            )
            unittest_all_verified = bool(
                verified_total > 0
                and all(r.get("unit_tests_passed") is True for r in results)
            )

            llm_cost, llm_requests = self._load_llm_usage(trial_dir)
            elapsed_seconds = self._extract_elapsed_seconds(paths.worker_log_path)
            builder_sidecar_api_calls = self._count_builder_sidecar_api_calls(trial_dir)
            (
                parsed_build_time,
                parsed_run_time,
                parsed_verify_time,
            ) = self._extract_phase_times(paths.worker_log_path)

            build_time = metadata.get("build_time")
            run_time = metadata.get("run_time")
            verify_time = parsed_verify_time
            if build_time is None:
                build_time = parsed_build_time
            if run_time is None:
                run_time = parsed_run_time

            rows.append(
                {
                    "crs": metadata.get("crs", ""),
                    "benchmark": metadata.get("benchmark", ""),
                    "harness": metadata.get("harness", ""),
                    "cpv": cpv_id or "",
                    "mode": run_mode or "",
                    "sanitizer": sanitizer or "",
                    "status": status,
                    "patch_generated_count": patch_generated,
                    "verified_valid_count": verified_valid,
                    "verified_total_count": verified_total,
                    "no_crash_all_verified": no_crash_all_verified,
                    "unittest_all_verified": unittest_all_verified,
                    "verification_build_failed": int(
                        summary.get("build_failed", 0) or 0
                    ),
                    "verification_pov_still_triggers": int(
                        summary.get("pov_still_triggers", 0) or 0
                    ),
                    "verification_test_failed": int(summary.get("test_failed", 0) or 0),
                    "verification_error": int(summary.get("error", 0) or 0),
                    "builder_sidecar_api_calls": builder_sidecar_api_calls,
                    "build_time": build_time,
                    "run_time": run_time,
                    "verify_time": verify_time,
                    "elapsed_seconds": elapsed_seconds,
                    "llm_cost_usd": llm_cost,
                    "llm_requests": llm_requests,
                    "trial_dir": str(trial_dir),
                }
            )

        fieldnames = [
            "crs",
            "benchmark",
            "harness",
            "cpv",
            "mode",
            "sanitizer",
            "status",
            "patch_generated_count",
            "verified_valid_count",
            "verified_total_count",
            "no_crash_all_verified",
            "unittest_all_verified",
            "verification_build_failed",
            "verification_pov_still_triggers",
            "verification_test_failed",
            "verification_error",
            "builder_sidecar_api_calls",
            "build_time",
            "run_time",
            "verify_time",
            "elapsed_seconds",
            "llm_cost_usd",
            "llm_requests",
            "trial_dir",
        ]
        self._write_csv_rows(out_path, rows, fieldnames)
        logger.debug(f"Generated patch analysis CSV: {out_path}")
        return out_path

    @staticmethod
    def _resolve_trial_status(trial_dir: Path) -> str:
        if (trial_dir / ".success").exists():
            return "finished"
        if (trial_dir / ".fail").exists():
            return "failed"
        if (trial_dir / ".started").exists():
            return "started"
        return "running"

    @staticmethod
    def _load_llm_usage(trial_dir: Path) -> tuple[float, int]:
        llm_path = trial_dir / "llm-usage.json"
        if not llm_path.exists():
            return 0.0, 0
        try:
            data = json.loads(llm_path.read_text())
            return (
                float(data.get("total_cost_usd", 0.0) or 0.0),
                int(data.get("request_count", 0) or 0),
            )
        except Exception:
            return 0.0, 0

    @staticmethod
    def _extract_elapsed_seconds(worker_log_path: Path) -> float | None:
        if not worker_log_path.exists():
            return None
        try:
            text = worker_log_path.read_text(errors="ignore")
        except Exception:
            return None

        matches = re.findall(
            r"\[Trial\s+\d+\]\s+Completed.* in ([0-9]+(?:\.[0-9]+)?)s", text
        )
        if not matches:
            return None
        try:
            return float(matches[-1])
        except ValueError:
            return None

    @staticmethod
    def _extract_phase_times(
        worker_log_path: Path,
    ) -> tuple[float | None, float | None, float | None]:
        """Extract build/run/verify durations from worker log timestamps.

        Returns:
            Tuple: (build_time, run_time, verify_time) in seconds.
        """
        if not worker_log_path.exists():
            return None, None, None

        try:
            lines = worker_log_path.read_text(errors="ignore").splitlines()
        except Exception:
            return None, None, None

        def parse_ts(line: str) -> datetime | None:
            # Log format begins with "YYYY-MM-DD HH:MM:SS | ..."
            if len(line) < 19:
                return None
            try:
                return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        build_start: datetime | None = None
        build_end: datetime | None = None
        verify_start: datetime | None = None
        end_time: datetime | None = None

        for line in lines:
            ts = parse_ts(line)
            if ts is None:
                continue
            if build_start is None and "phase -> building" in line:
                build_start = ts
            if build_end is None and "Build complete for " in line:
                build_end = ts
            if verify_start is None and "phase -> verifying" in line:
                verify_start = ts
            if (
                "Evaluation completed:" in line
                or "Benchmark evaluation failed:" in line
            ):
                end_time = ts

        build_time = (
            (build_end - build_start).total_seconds()
            if build_start is not None and build_end is not None
            else None
        )
        run_time = (
            (verify_start - build_end).total_seconds()
            if build_end is not None and verify_start is not None
            else None
        )
        verify_time = (
            (end_time - verify_start).total_seconds()
            if verify_start is not None and end_time is not None
            else None
        )
        return build_time, run_time, verify_time

    @staticmethod
    def _count_builder_sidecar_api_calls(trial_dir: Path) -> int:
        """Count sidecar builder API calls from inc-builder stdout logs.

        Counts only HTTP POST access-log lines (build/run-pov/run-test calls), and
        ignores health/status polling GET requests.
        """
        services_dir = trial_dir / "output" / "logs" / "services"
        log_paths = sorted(services_dir.glob("*inc-builder-*.stdout.log"))

        # Backward-compatible fallback for older layouts without services logs.
        if not log_paths:
            log_paths = sorted(
                (trial_dir / "output" / "logs" / "crs").glob(
                    "**/*inc-builder-*.stdout.log"
                )
            )

        api_calls = 0
        for log_path in log_paths:
            try:
                for line in log_path.read_text(errors="ignore").splitlines():
                    if '"POST /' in line and "HTTP/" in line:
                        api_calls += 1
            except Exception:
                continue

        return api_calls

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
            # Early stop analysis
            "total_cpvs": trial_metrics.get("total_cpvs", 0),
            "cpvs_found": trial_metrics.get("cpvs_found_count", 0),
            "all_cpvs_found": trial_metrics.get("all_cpvs_found", False),
            "early_stop_time": trial_metrics.get("early_stop_time", ""),
            "early_stop_cost": trial_metrics.get("early_stop_cost", ""),
            "time_saved": trial_metrics.get("time_saved", ""),
            "cost_saved": trial_metrics.get("cost_saved", ""),
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
