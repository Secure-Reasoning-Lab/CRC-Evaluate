"""Report generation orchestrator - coordinates all reporting components."""

from pathlib import Path
from typing import Any

from crsbench.reporting.errors import ReportGenerationError
from crsbench.reporting.generators import (
    CSVReportGenerator,
    HTMLReportGenerator,
    JSONReportGenerator,
)
from crsbench.reporting.metrics import MetricsAggregator
from crsbench.reporting.models import ExperimentMetrics, TrialInfo, TrialMetrics
from crsbench.reporting.snapshot_loader import SnapshotLoader, discover_trials
from crsbench.reporting.validator import ExperimentValidator
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class ReportGenerator:
    """Main orchestrator for report generation.

    This class coordinates all components of the reporting module:
    - SnapshotLoader: Load and parse snapshot archives
    - MetricsAggregator: Compute metrics from snapshots
    - ExperimentValidator: Validate experiment completeness
    - JSONReportGenerator: Generate JSON reports
    - HTMLReportGenerator: Generate HTML reports

    Example:
        generator = ReportGenerator(output_dir=Path("./reports"))

        # Generate report for an experiment
        result = generator.generate_experiment_report(
            experiment_dir=Path("./experiment_filestore/test-exp"),
        )

        # Validate experiment only
        validation = generator.validate_experiment(
            experiment_dir=Path("./experiment_filestore/test-exp")
        )
    """

    def __init__(self, output_dir: Path, benchmarks_root: Path):
        """Initialize the report generator.

        Args:
            output_dir: Directory to write reports to
            benchmarks_root: Path to benchmarks root directory for ground truth comparison
        """
        self.output_dir = output_dir
        self.benchmarks_root = benchmarks_root
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.snapshot_loader = SnapshotLoader()
        self.metrics_aggregator = MetricsAggregator()
        self.validator = ExperimentValidator()
        self.json_generator = JSONReportGenerator(output_dir)
        self.html_generator = HTMLReportGenerator(output_dir)
        self.csv_generator = CSVReportGenerator(output_dir)

    def _get_cpv_info(self, trial_info: TrialInfo) -> tuple[int, list[str]]:
        """Get CPV count and IDs for a trial from ground truth meta.yaml.

        Args:
            trial_info: Trial information containing benchmark and harness

        Returns:
            Tuple of (total_cpvs, cpv_ids). Returns (0, []) if unable to load.
        """
        from crsbench.validation.meta_adapter import MetaYamlAdapter

        # Load meta.yaml for this benchmark
        benchmark_path = self.benchmarks_root / trial_info.benchmark

        if not benchmark_path.exists():
            logger.warning(
                f"Benchmark directory not found: {benchmark_path} "
                f"(for trial {trial_info.trial_num})"
            )
            return 0, []

        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if adapter is None:
            logger.warning(
                f"Failed to load meta.yaml for benchmark: {trial_info.benchmark} "
                f"(for trial {trial_info.trial_num})"
            )
            return 0, []

        # Get harness and extract CPV IDs
        harness = adapter.get_harness(trial_info.harness)
        if harness is None:
            logger.warning(
                f"Harness not found in meta.yaml: {trial_info.harness} "
                f"in benchmark {trial_info.benchmark} "
                f"(for trial {trial_info.trial_num})"
            )
            return 0, []

        # Extract CPV IDs from vulns
        if not harness.vulns:
            return 0, []

        cpv_ids = [v.vuln_keyword for v in harness.vulns]
        total_cpvs = len(cpv_ids)
        logger.debug(
            f"Trial {trial_info.trial_num}: "
            f"{trial_info.benchmark}/{trial_info.harness} has {total_cpvs} CPVs "
            f"({cpv_ids})"
        )
        return total_cpvs, cpv_ids

    def generate_experiment_report(
        self,
        experiment_dir: Path,
        *,
        format: str = "both",
        skip_incomplete: bool = True,
    ) -> dict[str, Path | list[Path]]:
        """Generate report for an entire experiment.

        Args:
            experiment_dir: Path to experiment directory
            format: Report format ("json", "html", "csv", "both", or "all")
            skip_incomplete: If True, skip incomplete trials

        Returns:
            Dict mapping report type to file path (or list of paths for CSV)

        Raises:
            ReportGenerationError: If report generation fails
        """
        logger.info(f"Generating report for experiment: {experiment_dir}")

        # Discover trials
        trials = discover_trials(experiment_dir)

        if not trials:
            raise ReportGenerationError(
                f"No trials found in experiment directory: {experiment_dir}"
            )

        # Filter to valid trials
        valid_trials = [t for t in trials if t.status == "valid"]

        if not valid_trials:
            if skip_incomplete:
                raise ReportGenerationError(
                    f"No valid trials found in experiment: {experiment_dir}"
                )
            logger.warning(
                "No valid trials found, but skip_incomplete=False. "
                "Report will be empty."
            )
            valid_trials = []

        logger.info(f"Processing {len(valid_trials)}/{len(trials)} valid trials")

        # Process each trial
        trial_metrics_list: list[TrialMetrics] = []
        trial_json_paths: list[Path] = []
        trial_html_paths: list[Path] = []

        for trial_info in valid_trials:
            try:
                # Load snapshots
                snapshots = self.snapshot_loader.load_trial_snapshots(
                    trial_info.trial_dir
                )

                if not snapshots:
                    logger.warning(
                        f"No snapshots found for trial {trial_info.trial_num}"
                    )
                    continue

                # Load ground truth CPV count and IDs from meta.yaml
                total_cpvs, cpv_ids = self._get_cpv_info(trial_info)

                # Aggregate metrics
                trial_metrics = self.metrics_aggregator.aggregate_trial(
                    trial_info=trial_info,
                    snapshots=snapshots,
                    total_cpvs=total_cpvs,
                    cpv_ids=cpv_ids,
                )
                trial_metrics_list.append(trial_metrics)

                # Generate trial reports
                if format in ("json", "both", "all"):
                    json_path = self.json_generator.generate_trial_report(
                        trial_metrics, snapshots
                    )
                    trial_json_paths.append(json_path)

                if format in ("html", "both", "all"):
                    html_path = self.html_generator.generate_trial_report(
                        trial_metrics, snapshots
                    )
                    trial_html_paths.append(html_path)

                if format in ("csv", "all"):
                    self.csv_generator.generate_trial_report(
                        trial_metrics.model_dump(), snapshots
                    )

            except Exception as e:
                logger.error(f"Failed to process trial {trial_info.trial_num}: {e}")
                if not skip_incomplete:
                    raise ReportGenerationError(
                        f"Failed to process trial {trial_info.trial_num}: {e}"
                    ) from e

        if not trial_metrics_list:
            raise ReportGenerationError("No trials could be processed successfully")

        # Aggregate experiment metrics
        experiment_metrics = self.metrics_aggregator.aggregate_experiment(
            experiment_dir=str(experiment_dir),
            trial_metrics_list=trial_metrics_list,
        )

        # Generate experiment reports
        result: dict[str, Path | list[Path]] = {}

        if format in ("json", "both", "all"):
            json_path = self.json_generator.generate_experiment_report(
                experiment_metrics, trial_json_paths
            )
            result["json"] = json_path

        if format in ("html", "both", "all"):
            html_path = self.html_generator.generate_experiment_report(
                experiment_metrics
            )
            result["html"] = html_path

        if format in ("csv", "all"):
            csv_paths = self.csv_generator.generate_experiment_report(
                experiment_metrics.model_dump()
            )
            result["csv"] = csv_paths

        logger.info(
            f"Report generation complete. Processed {len(trial_metrics_list)} trials."
        )

        return result

    def generate_trial_report(
        self,
        trial_dir: Path,
        *,
        format: str = "both",
    ) -> dict[str, Path | list[Path]]:
        """Generate report for a single trial.

        Args:
            trial_dir: Path to trial directory
            format: Report format ("json", "html", "csv", "both", or "all")

        Returns:
            Dict mapping report type to file path (or list of paths for CSV)

        Raises:
            ReportGenerationError: If report generation fails
        """
        import json
        import re

        from pydantic import ValidationError

        from crsbench.reporting.models import TrialMetadataFile

        # Extract trial number from directory name
        match = re.match(r"trial-(\d+)", trial_dir.name)
        trial_num = int(match.group(1)) if match else 1

        # Load trial metadata
        metadata_path = trial_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata_dict = json.loads(metadata_path.read_text())
                metadata = TrialMetadataFile.model_validate(metadata_dict)

                trial_info = TrialInfo(
                    trial_dir=trial_dir,
                    trial_num=trial_num,
                    crs=metadata.crs,
                    benchmark=metadata.benchmark,
                    harness=metadata.harness,
                    mode=metadata.mode,
                    status="valid",
                    metadata=metadata,
                )
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Failed to parse metadata.json: {e}")
                trial_info = TrialInfo(
                    trial_dir=trial_dir,
                    trial_num=trial_num,
                    status="invalid_metadata",
                    error=str(e),
                )
        else:
            trial_info = TrialInfo(
                trial_dir=trial_dir,
                trial_num=trial_num,
                status="missing_metadata",
                error="metadata.json not found",
            )

        logger.info(f"Generating report for trial: {trial_num}")

        # Load snapshots
        snapshots = self.snapshot_loader.load_trial_snapshots(trial_dir)

        if not snapshots:
            raise ReportGenerationError(
                f"No snapshots found in trial directory: {trial_dir}"
            )

        # Load ground truth CPV count and IDs from meta.yaml
        total_cpvs, cpv_ids = self._get_cpv_info(trial_info)

        # Aggregate metrics
        trial_metrics = self.metrics_aggregator.aggregate_trial(
            trial_info=trial_info,
            snapshots=snapshots,
            total_cpvs=total_cpvs,
            cpv_ids=cpv_ids,
        )

        # Generate reports
        result: dict[str, Path | list[Path]] = {}

        if format in ("json", "both", "all"):
            json_path = self.json_generator.generate_trial_report(
                trial_metrics, snapshots
            )
            result["json"] = json_path

        if format in ("html", "both", "all"):
            html_path = self.html_generator.generate_trial_report(
                trial_metrics, snapshots
            )
            result["html"] = html_path

        if format in ("csv", "all"):
            csv_paths = self.csv_generator.generate_trial_report(
                trial_metrics.model_dump(), snapshots
            )
            result["csv"] = csv_paths

        logger.info(f"Trial report generation complete: trial-{trial_num}")

        return result

    def validate_experiment(self, experiment_dir: Path) -> str:
        """Validate experiment completeness and return report.

        Args:
            experiment_dir: Path to experiment directory

        Returns:
            Completeness report string
        """
        validation_result = self.validator.validate_experiment_completeness(
            experiment_dir
        )
        return self.validator.generate_completeness_report(validation_result)

    def get_experiment_metrics(
        self,
        experiment_dir: Path,
    ) -> ExperimentMetrics:
        """Get aggregated metrics for an experiment without generating reports.

        Args:
            experiment_dir: Path to experiment directory

        Returns:
            ExperimentMetrics object
        """
        # Discover and process trials
        trials = discover_trials(experiment_dir)
        valid_trials = [t for t in trials if t.status == "valid"]

        trial_metrics_list: list[TrialMetrics] = []

        for trial_info in valid_trials:
            snapshots = self.snapshot_loader.load_trial_snapshots(trial_info.trial_dir)

            if snapshots:
                # Load ground truth CPV count and IDs
                total_cpvs, cpv_ids = self._get_cpv_info(trial_info)

                trial_metrics = self.metrics_aggregator.aggregate_trial(
                    trial_info=trial_info,
                    snapshots=snapshots,
                    total_cpvs=total_cpvs,
                    cpv_ids=cpv_ids,
                )
                trial_metrics_list.append(trial_metrics)

        return self.metrics_aggregator.aggregate_experiment(
            experiment_dir=str(experiment_dir),
            trial_metrics_list=trial_metrics_list,
        )

    def dry_run(self, experiment_dir: Path) -> dict[str, Any]:
        """Preview what reports would be generated without actually generating them.

        Args:
            experiment_dir: Path to experiment directory

        Returns:
            Dictionary containing preview information
        """

        # Discover trials
        trials = discover_trials(experiment_dir)
        valid_trials = [t for t in trials if t.status == "valid"]

        # Collect trial info
        trial_previews = []
        total_snapshots = 0

        for trial_info in valid_trials:
            snapshots = self.snapshot_loader.load_trial_snapshots(trial_info.trial_dir)
            snapshot_count = len(snapshots)
            total_snapshots += snapshot_count

            trial_previews.append(
                {
                    "trial_num": f"trial-{trial_info.trial_num}",
                    "crs": trial_info.crs or "unknown",
                    "benchmark": trial_info.benchmark or "unknown",
                    "harness": trial_info.harness or "unknown",
                    "mode": trial_info.mode or "unknown",
                    "snapshot_count": snapshot_count,
                }
            )

        # Count unique CRS and benchmarks
        unique_crs = len({t["crs"] for t in trial_previews})
        unique_benchmarks = len({t["benchmark"] for t in trial_previews})

        # Calculate CSV file row counts
        trial_count = len(trial_previews)
        crs_summary_count = unique_crs
        benchmark_summary_count = unique_benchmarks
        time_series_count = total_snapshots
        combined_count = (
            trial_count
            + crs_summary_count
            + benchmark_summary_count
            + time_series_count
        )

        return {
            "trials": trial_previews,
            "csv_files": {
                "trial_summary.csv": {
                    "rows": trial_count,
                    "columns": [
                        "trial_num",
                        "crs",
                        "benchmark",
                        "harness",
                        "mode",
                        "total_povs",
                        "unique_povs",
                        "total_patches",
                        "unique_patches",
                        "total_llm_cost",
                        "total_llm_tokens",
                        "total_time",
                        "time_to_first_pov",
                        "snapshot_count",
                    ],
                },
                "crs_summary.csv": {
                    "rows": crs_summary_count,
                    "columns": [
                        "crs",
                        "trial_count",
                        "avg_povs",
                        "avg_patches",
                        "avg_cost",
                        "total_cost",
                        "total_povs",
                    ],
                },
                "benchmark_summary.csv": {
                    "rows": benchmark_summary_count,
                    "columns": [
                        "benchmark",
                        "trial_count",
                        "avg_povs",
                        "avg_patches",
                        "avg_time_to_first_pov",
                        "total_cost",
                    ],
                },
                "time_series.csv": {
                    "rows": time_series_count,
                    "columns": [
                        "trial_num",
                        "crs",
                        "benchmark",
                        "elapsed_time",
                        "cumulative_povs",
                        "cumulative_patches",
                        "llm_tokens",
                        "llm_cost",
                    ],
                },
                "combined_report.csv": {
                    "rows": combined_count,
                    "columns": [
                        "record_type",
                        "trial_num",
                        "crs",
                        "benchmark",
                        "harness",
                        "mode",
                        "total_povs",
                        "unique_povs",
                        "total_patches",
                        "unique_patches",
                        "total_llm_cost",
                        "total_llm_tokens",
                        "total_time",
                        "time_to_first_pov",
                        "snapshot_count",
                        "trial_count",
                        "avg_povs",
                        "avg_patches",
                        "avg_cost",
                        "total_cost",
                        "avg_time_to_first_pov",
                        "elapsed_time",
                        "cumulative_povs",
                        "cumulative_patches",
                        "llm_tokens",
                        "llm_cost",
                    ],
                },
            },
        }
