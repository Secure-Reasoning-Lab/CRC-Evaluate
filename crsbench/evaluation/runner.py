"""Main benchmark runner for CRS evaluation."""

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_executor import CRSExecutor, StubCRSExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.results import EvaluationReport, HarnessResult, ResultCollector
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.utils.logger import get_logger
from crsbench.validation import ValidationResult, VerificationEngine, validate_benchmark
from crsbench.validation import VerificationResult as VerifResult
from crsbench.validation.schemas import BenchmarkConfig, BenchmarkHarness, HarnessFile

# Set up logging
logger = get_logger(__name__)


class EvaluationError(Exception):
    """Exception raised during benchmark evaluation."""


class EvaluationResult:
    """Result from a benchmark evaluation."""

    def __init__(
        self,
        report: EvaluationReport,
        validation_result: ValidationResult,
        verification_results: Optional[List] = None,
    ):
        self.report = report
        self.validation_result = validation_result
        self.verification_results = verification_results or []

    @property
    def is_valid(self) -> bool:
        """Whether the benchmark configuration was valid."""
        return self.validation_result.is_valid

    @property
    def success_rate(self) -> float:
        """POV detection success rate."""
        return self.report.success_rate

    @property
    def total_povs(self) -> int:
        """Total number of POVs evaluated."""
        return self.report.total_povs

    @property
    def povs_found(self) -> int:
        """Number of POVs successfully detected."""
        return self.report.povs_found


class BenchmarkRunner:
    """Main class for running benchmark evaluations."""

    def __init__(
        self,
        crs_executor: Optional[CRSExecutor] = None,
        snapshot_period: Optional[int] = None,
    ):
        """Initialize benchmark runner.

        Args:
            crs_executor: CRS executor instance. If None, uses stub executor.
            snapshot_period: Snapshot interval in seconds (0 or None to disable)
        """
        self.crs_executor = crs_executor or StubCRSExecutor()
        self.snapshot_period = snapshot_period
        self.logger = get_logger(__name__)

    def run_benchmark(
        self,
        benchmark_harness: "BenchmarkHarness",
        mode: Optional[str] = None,
        crs_config: Optional[Dict[str, Any]] = None,
        trial_output_dir: Optional[Path] = None,
        oss_fuzz_path: Optional[Path] = None,
        *,
        skip_verification: bool = False,
    ) -> EvaluationResult:
        """Run a complete benchmark evaluation for a specific harness.

        Args:
            benchmark_harness: BenchmarkHarness object with benchmark path and harness info
            mode: Evaluation mode ('delta', 'full', or 'auto' to detect)
            crs_config: Configuration for CRS executor
            trial_output_dir: Trial output directory for snapshots (required if snapshots enabled)
            skip_verification: Skip POV verification (default: False, verification enabled)
            oss_fuzz_path: Path to oss-fuzz directory (required for POV verification)

        Returns:
            EvaluationResult: Complete evaluation results

        Raises:
            EvaluationError: If evaluation fails
        """
        # Extract components from benchmark_harness
        benchmark_path = benchmark_harness.path
        harness = benchmark_harness.harness

        self.logger.info(f"Starting benchmark evaluation: {benchmark_path}")

        # Record trial start time
        trial_start_time = time.time()

        # Validate snapshot configuration
        if self.snapshot_period and self.snapshot_period > 0:
            if not trial_output_dir:
                raise EvaluationError(
                    "trial_output_dir is required when snapshots are enabled"
                )
            if not trial_output_dir.exists():
                raise EvaluationError(
                    f"trial_output_dir does not exist: {trial_output_dir}"
                )

        try:
            # Step 1: Validate benchmark configuration
            self.logger.info("Validating benchmark configuration...")
            validation_result = validate_benchmark(benchmark_path)

            if not validation_result.is_valid:
                self.logger.error("Benchmark configuration is invalid:")
                for error in validation_result.errors:
                    self.logger.error(f"  - {error.message}")
                # Continue with evaluation but mark as invalid
                # This allows for partial evaluation and debugging

            # Step 2: Parse configuration
            config = self._load_benchmark_config(benchmark_path)

            # Step 3: Determine evaluation mode
            evaluation_mode = self._determine_evaluation_mode(config, mode)
            self.logger.info(f"Evaluation mode: {evaluation_mode}")

            # Step 4: Configure CRS
            if crs_config:
                self.logger.info("Configuring CRS...")
                self.crs_executor.configure_crs(crs_config)

            # Step 5: Set up result collector
            collector = ResultCollector(str(benchmark_path), evaluation_mode)

            # Step 6: Set commit information
            if evaluation_mode == "delta" and config.delta_mode:
                collector.set_commits(
                    config.delta_mode.base_commit, config.delta_mode.ref_commit
                )
            elif evaluation_mode == "full" and config.full_mode:
                collector.set_commits(config.full_mode.base_commit)

            if crs_config:
                collector.set_crs_config(crs_config)

            # Step 6.5: Pre-build CRS (before snapshot starts)
            if (
                isinstance(self.crs_executor, (CRSBugFindingExecutor, CRSPatchExecutor))
                and trial_output_dir
            ):
                self.logger.info("Pre-building CRS before snapshot period...")
                self.crs_executor.build_crs(benchmark_path, trial_output_dir)

            # Step 7: Run evaluation on harness
            harness_result, verification_results = self._run_harness_evaluation(
                harness=harness,
                benchmark_path=benchmark_path,
                trial_output_dir=trial_output_dir or Path(),
                trial_start_time=trial_start_time,
                oss_fuzz_path=oss_fuzz_path,
                skip_verification=skip_verification,
            )
            collector.add_harness_result(harness_result)

            # Step 9: Set POV statistics from verification results
            if verification_results:
                collector.set_pov_stats(verification_results)

            # Step 10: Generate final report
            report = collector.finalize_report()

            self.logger.info(
                f"Evaluation completed: {report.povs_found}/{report.total_povs} POVs detected "
                f"({report.success_rate:.1%} success rate)"
            )

            return EvaluationResult(report, validation_result, verification_results)

        except Exception as e:
            self.logger.error(f"Benchmark evaluation failed: {str(e)}")
            raise EvaluationError(f"Failed to evaluate benchmark: {str(e)}") from e

    def _load_benchmark_config(self, benchmark_path: Path) -> BenchmarkConfig:
        """Load benchmark configuration from validation result or file."""
        # For now, we need to re-parse since validation doesn't return the config
        # This is a limitation we could improve in the validation module later
        import yaml

        meta_yaml_path = self._resolve_meta_yaml_path(benchmark_path)

        if not meta_yaml_path.exists():
            raise EvaluationError(f"meta.yaml not found at {meta_yaml_path}")

        with meta_yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        try:
            return BenchmarkConfig(**data)
        except Exception as e:
            # If validation failed, create minimal config to continue
            self.logger.warning(f"Failed to parse config, using minimal fallback: {e}")
            from crsbench.validation.schemas import FullMode, HarnessFile

            dummy_harness = HarnessFile(name="dummy", path="/src/project/dummy.c")
            dummy_full_mode = FullMode(base_commit="abc123def456")
            return BenchmarkConfig(
                harness_files=[dummy_harness], full_mode=dummy_full_mode
            )

    def _resolve_meta_yaml_path(self, path: Path) -> Path:
        """Resolve the path to meta.yaml file."""
        if path.is_file() and path.name == "meta.yaml":
            return path
        if path.is_dir():
            # Look for meta.yaml in .aixcc subdirectory first
            aixcc_path = path / ".aixcc" / "meta.yaml"
            if aixcc_path.exists():
                return aixcc_path

            # Look for meta.yaml in root directory
            root_path = path / "meta.yaml"
            if root_path.exists():
                return root_path

            # Return expected path even if it doesn't exist
            return aixcc_path
        # Assume it's meant to be meta.yaml file
        return path

    def _determine_evaluation_mode(
        self, config: BenchmarkConfig, mode: Optional[str]
    ) -> str:
        """Determine the evaluation mode to use."""
        if mode and mode in ["delta", "full"]:
            # Validate that the requested mode is available
            if mode == "delta" and not config.delta_mode:
                raise EvaluationError(
                    "Delta mode requested but not configured in benchmark"
                )
            if mode == "full" and not config.full_mode:
                raise EvaluationError(
                    "Full mode requested but not configured in benchmark"
                )
            return mode
        if mode == "auto" or mode is None:
            # Auto-detect based on available configuration
            if config.delta_mode:
                return "delta"
            if config.full_mode:
                return "full"
            raise EvaluationError(
                "No evaluation mode available in benchmark configuration"
            )
        raise EvaluationError(f"Invalid evaluation mode: {mode}")

    def _run_harness_evaluation(
        self,
        harness: HarnessFile,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        oss_fuzz_path: Optional[Path],
        *,
        skip_verification: bool,
    ) -> tuple[HarnessResult, List[VerifResult]]:
        """Run evaluation for a single harness with snapshot management and verification.

        Args:
            harness: Harness configuration
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Unix timestamp when trial started
            oss_fuzz_path: Path to oss-fuzz directory (for verification)
            skip_verification: Skip POV verification

        Returns:
            Tuple of (HarnessResult, List of verification results)
        """
        snapshot_manager = None
        snapshot_thread = None
        verification_results: List[VerifResult] = []
        harness_result = None

        self.logger.info(f"Evaluating harness: {harness.name}")

        try:
            # Start snapshot thread for this harness
            if self.snapshot_period and self.snapshot_period > 0:
                self.logger.info(
                    f"Starting snapshot manager for harness '{harness.name}' "
                    f"(period={self.snapshot_period}s)"
                )
                snapshot_manager = SnapshotManager(
                    trial_dir=trial_output_dir,
                    snapshot_period=self.snapshot_period,
                    trial_start_time=trial_start_time,
                )
                snapshot_thread = threading.Thread(
                    target=snapshot_manager.run, daemon=True
                )
                snapshot_thread.start()

            # Create callback to set CRS run start time
            def on_run_start() -> None:
                if snapshot_manager:
                    snapshot_manager.set_crs_run_start_time(time.time())

            # Run CRS on this harness
            crs_result = self.crs_executor.run_crs(
                benchmark_path=benchmark_path,
                harness=harness,
                trial_output_dir=trial_output_dir,
                on_run_start=on_run_start,
            )

            # Create harness result
            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=crs_result.execution_time,
                build_successful=crs_result.success,
                build_output=crs_result.output,
            )

        except Exception as e:
            self.logger.error(f"Failed to evaluate harness '{harness.name}': {str(e)}")
            # Create error result
            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=0.0,
                build_successful=False,
                build_output=f"Error: {str(e)}",
            )

        finally:
            # Capture final snapshot and stop snapshot thread
            if snapshot_manager:
                self.logger.info(
                    f"Capturing final snapshot for harness '{harness.name}'..."
                )
                try:
                    snapshot_manager.capture_snapshot()
                except Exception as e:
                    self.logger.warning(f"Failed to capture final snapshot: {e}")

                self.logger.info("Stopping snapshot manager...")
                snapshot_manager.stop()
                if snapshot_thread and snapshot_thread.is_alive():
                    snapshot_thread.join(timeout=5.0)
                    if snapshot_thread.is_alive():
                        self.logger.warning(
                            "Snapshot thread did not stop within timeout"
                        )

        # Verify POVs AFTER snapshot thread has stopped and final snapshot captured
        if (
            harness_result
            and harness_result.build_successful
            and not skip_verification
            and isinstance(self.crs_executor, CRSBugFindingExecutor)
            and oss_fuzz_path
        ):
            crs_output_dir = trial_output_dir / "output"
            verification_results = self._verify_povs(
                benchmark_path=benchmark_path,
                crs_output_dir=crs_output_dir,
                oss_fuzz_path=oss_fuzz_path,
                harness_name=harness.name,
            )

        return harness_result, verification_results

    def _verify_povs(
        self,
        benchmark_path: Path,
        crs_output_dir: Path,
        oss_fuzz_path: Path,
        harness_name: str,
    ) -> List[VerifResult]:
        """Verify CRS-generated POVs against benchmark variants for a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            crs_output_dir: Path to CRS output directory containing POVs
            oss_fuzz_path: Path to oss-fuzz directory
            harness_name: Name of the harness to verify POVs for

        Returns:
            List of verification results for this harness
        """
        try:
            self.logger.info(f"Starting POV verification for harness: {harness_name}")
            engine = VerificationEngine(
                oss_fuzz_path=oss_fuzz_path,
                timeout=120,
                dedup_strategy="patch-based",  # TODO: make it configurable
            )
            pov_dir = crs_output_dir / "povs"
            return engine.verify_benchmark(
                benchmark_path=benchmark_path,
                pov_dir=pov_dir,
                deduplicate=True,  # TODO: configurable?
                harness_filter=harness_name,
            )
        except Exception as e:
            self.logger.error(
                f"POV verification failed for harness '{harness_name}': {e}",
                exc_info=True,
            )
            return []
