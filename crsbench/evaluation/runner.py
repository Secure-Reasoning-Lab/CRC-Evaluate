"""Main benchmark runner for CRS evaluation."""

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor

if TYPE_CHECKING:
    from crsbench.evaluation.litellm_tracker import LiteLLMTracker
from crsbench.evaluation.crs_executor import CRSExecutor, StubCRSExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.results import EvaluationReport, HarnessResult, ResultCollector
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.evaluation.verification import PatchBasedDedup, VerificationEngine
from crsbench.evaluation.verification import PovVerificationResult as VerifResult
from crsbench.evaluation.verification.models import (
    PatchVerificationOutput,
    PatchVerificationResult,
)
from crsbench.evaluation.verification.patch import (
    PatchVerificationEngine,
    PatchVerificationManager,
)
from crsbench.evaluation.verification.pov import (
    POVVerificationConfig,
    POVVerificationManager,
)
from crsbench.utils.logger import get_logger
from crsbench.validation import ValidationResult, validate_benchmark
from crsbench.validation.schemas import BenchmarkConfig, BenchmarkHarness, HarnessFile

# Set up logging
logger = get_logger(__name__)


class EvaluationError(Exception):
    """Exception raised during benchmark evaluation."""


class BenchmarkFormatError(Exception):
    """Exception raised when benchmark configuration is invalid."""

    def __init__(self, message: str, validation_result: ValidationResult):
        self.validation_result = validation_result
        super().__init__(message)


class EvaluationResult:
    """Result from a benchmark evaluation."""

    def __init__(
        self,
        report: EvaluationReport,
        verification_results: Optional[list] = None,
    ):
        self.report = report
        self.verification_results = verification_results or []

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

    @property
    def success(self) -> bool:
        """Return True if evaluation was successful."""
        return self.report.success


class BenchmarkRunner:
    """Main class for running benchmark evaluations."""

    def __init__(
        self,
        crs_executor: Optional[CRSExecutor] = None,
        snapshot_period: Optional[int] = None,
        *,
        coverage_enabled: bool = False,
        coverage_saturation_time: int = 21600,
        coverage_early_stop: bool = False,
        pov_early_stop: bool = False,
        per_pov_verify_timeout: int = 180,
        verify_timeout: int = 7200,
        oss_fuzz_path: Optional[Path] = None,
        on_build_start: Optional[Callable[[], None]] = None,
        on_run_start: Optional[Callable[[], None]] = None,
        on_verification_start: Optional[Callable[[], None]] = None,
        llm_tracker: Optional["LiteLLMTracker"] = None,
        llm_api_key: Optional[str] = None,
        llm_trial_id: Optional[str] = None,
        build_workers: Optional[int] = None,
        verify_workers: Optional[int] = None,
        redis_host: Optional[str] = None,
        experiment_name: Optional[str] = None,
    ):
        """Initialize benchmark runner.

        Args:
            crs_executor: CRS executor instance. If None, uses stub executor.
            snapshot_period: Snapshot interval in seconds (0 or None to disable)
            coverage_enabled: Enable coverage collection during trials
            coverage_saturation_time: Seconds without new coverage to detect saturation
            coverage_early_stop: Terminate trial early when coverage saturation is detected
            pov_early_stop: Terminate trial early when all CPVs for harness are found
            per_pov_verify_timeout: Timeout in seconds for each single POV verification (default: 180)
            verify_timeout: Overall budget in seconds for the verification phase (default: 7200)
            oss_fuzz_path: Path to oss-fuzz directory (required for coverage and POV verification)
            on_build_start: Callback invoked when CRS build phase starts
            on_run_start: Callback invoked when CRS run phase starts
            on_verification_start: Callback invoked when verification phase starts
            llm_tracker: Optional LiteLLMTracker for querying LLM usage during snapshots
            llm_api_key: Optional trial-specific API key for LLM tracking
            llm_trial_id: Optional trial identifier for LLM usage files
            build_workers: Number of parallel workers for building variants
            verify_workers: Number of parallel workers for POV/patch verification
            redis_host: Redis server hostname for async POV verification
            experiment_name: Experiment name for async verify queue naming
        """
        self.crs_executor = crs_executor or StubCRSExecutor()
        self.snapshot_period = snapshot_period
        self.coverage_enabled = coverage_enabled
        self.coverage_saturation_time = coverage_saturation_time
        self.coverage_early_stop = coverage_early_stop
        self.pov_early_stop = pov_early_stop
        self.per_pov_verify_timeout = per_pov_verify_timeout
        self.verify_timeout = verify_timeout
        self.oss_fuzz_path = oss_fuzz_path
        self.on_build_start = on_build_start
        self.on_run_start = on_run_start
        self.on_verification_start = on_verification_start
        self.llm_tracker = llm_tracker
        self.llm_api_key = llm_api_key
        self.llm_trial_id = llm_trial_id
        self.build_workers = build_workers
        self.verify_workers = verify_workers
        self.redis_host = redis_host
        self.experiment_name = experiment_name
        self.logger = get_logger(__name__)

        if coverage_early_stop:
            if not coverage_enabled:
                self.logger.warning(
                    "coverage_early_stop=True requires coverage_enabled=True. "
                    "Early stop will NOT be active."
                )
            else:
                self.logger.info(
                    f"Coverage early stop enabled: will terminate after "
                    f"{coverage_saturation_time}s without new coverage"
                )

        if pov_early_stop:
            if not oss_fuzz_path:
                self.logger.warning(
                    "pov_early_stop=True requires oss_fuzz_path to be set. "
                    "Early stop will NOT be active."
                )
            else:
                self.logger.info(
                    "POV early stop enabled: will terminate when all CPVs for harness are found"
                )

    def run_benchmark(
        self,
        benchmark_harness: "BenchmarkHarness",
        mode: Optional[str] = None,
        crs_config: Optional[dict[str, Any]] = None,
        trial_output_dir: Optional[Path] = None,
        oss_fuzz_path: Optional[Path] = None,
        *,
        skip_verification: bool = False,
    ) -> EvaluationResult:
        """Run a complete benchmark evaluation for a specific harness."""
        benchmark_path = benchmark_harness.path
        harness = benchmark_harness.harness
        trial_start_time = time.time()

        self.logger.info(f"Starting benchmark evaluation: {benchmark_path}")
        self._validate_snapshot_config(trial_output_dir)

        try:
            # Setup phase
            validation_result = self._validate_benchmark(benchmark_path)
            if not validation_result.is_valid:
                errors_str = "; ".join(e.message for e in validation_result.errors)
                raise BenchmarkFormatError(
                    f"Invalid benchmark format: {errors_str}",
                    validation_result,
                )
            config = self._load_benchmark_config(benchmark_path)
            evaluation_mode = self._determine_evaluation_mode(config, mode)
            collector = self._setup_result_collector(
                benchmark_path, config, evaluation_mode, crs_config
            )
            self._pre_build_crs(benchmark_path, trial_output_dir)

            # Execution phase
            harness_result, pov_verification_results, patch_verification_results = (
                self._run_harness_evaluation(
                    harness=harness,
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir or Path(),
                    trial_start_time=trial_start_time,
                    oss_fuzz_path=oss_fuzz_path,
                    skip_verification=skip_verification,
                )
            )
            collector.add_harness_result(harness_result)

            # Result collection phase
            self._collect_crs_results(
                collector=collector,
                trial_output_dir=trial_output_dir,
                pov_verification_results=pov_verification_results,
                patch_verification_results=patch_verification_results,
            )

            # Finalize
            report = collector.finalize_report()
            self._log_evaluation_summary(report)

            return EvaluationResult(report, pov_verification_results)

        except BenchmarkFormatError:
            # Let validation errors propagate unchanged
            raise
        except Exception as e:
            self.logger.error(f"Benchmark evaluation failed: {str(e)}")
            raise EvaluationError(f"Failed to evaluate benchmark: {str(e)}") from e

    def _validate_snapshot_config(self, trial_output_dir: Optional[Path]) -> None:
        """Validate snapshot configuration."""
        if self.snapshot_period and self.snapshot_period > 0:
            if not trial_output_dir:
                raise EvaluationError(
                    "trial_output_dir is required when snapshots are enabled"
                )
            if not trial_output_dir.exists():
                raise EvaluationError(
                    f"trial_output_dir does not exist: {trial_output_dir}"
                )

    def _validate_benchmark(self, benchmark_path: Path) -> ValidationResult:
        """Validate benchmark configuration."""
        self.logger.info("Validating benchmark configuration...")
        validation_result = validate_benchmark(benchmark_path)

        if not validation_result.is_valid:
            self.logger.error("Benchmark configuration is invalid:")
            for error in validation_result.errors:
                self.logger.error(f"  - {error.message}")

        return validation_result

    def _setup_result_collector(
        self,
        benchmark_path: Path,
        config: BenchmarkConfig,
        evaluation_mode: str,
        crs_config: Optional[dict[str, Any]],
    ) -> ResultCollector:
        """Set up result collector with configuration."""
        self.logger.info(f"Evaluation mode: {evaluation_mode}")

        if crs_config:
            self.logger.info("Configuring CRS...")
            self.crs_executor.configure_crs(crs_config)

        collector = ResultCollector(str(benchmark_path), evaluation_mode)

        if evaluation_mode == "delta" and config.delta_mode:
            collector.set_commits(
                config.delta_mode.base_commit, config.delta_mode.ref_commit
            )
        elif evaluation_mode == "full" and config.full_mode:
            collector.set_commits(config.full_mode.base_commit)

        if crs_config:
            collector.set_crs_config(crs_config)

        return collector

    def _pre_build_crs(
        self, benchmark_path: Path, trial_output_dir: Optional[Path]
    ) -> None:
        """Pre-build CRS before snapshot starts."""
        if (
            isinstance(self.crs_executor, (CRSBugFindingExecutor, CRSPatchExecutor))
            and trial_output_dir
        ):
            self.logger.info("Pre-building CRS before snapshot period...")
            # Signal that CRS build is starting (idempotent)
            if self.on_build_start:
                self.on_build_start()
            self.crs_executor.build_crs(benchmark_path, trial_output_dir)

    def _collect_crs_results(
        self,
        collector: ResultCollector,
        trial_output_dir: Optional[Path],
        pov_verification_results: list[VerifResult],
        patch_verification_results: list[PatchVerificationResult],
    ) -> None:
        """Collect results based on CRS type.

        Similar to how POV stats are derived from verification results,
        patch stats are also derived from verification results.
        """
        if isinstance(self.crs_executor, CRSBugFindingExecutor):
            if pov_verification_results:
                collector.set_pov_stats(pov_verification_results)

        elif isinstance(self.crs_executor, CRSPatchExecutor) and trial_output_dir:
            if patch_verification_results:
                # Get total_input_povs from input directory
                povs_dir = trial_output_dir / "crs-input" / "povs"
                total_input_povs = (
                    len(list(povs_dir.iterdir())) if povs_dir.exists() else 0
                )
                # Set all patch stats from verification results (like POV)
                collector.set_patch_stats(total_input_povs, patch_verification_results)
                self.logger.info(
                    f"Patch verification: {len(patch_verification_results)} patches "
                    f"from {total_input_povs} input POVs"
                )
                self._save_patch_verification_results(
                    trial_output_dir, patch_verification_results, total_input_povs
                )

    def _log_evaluation_summary(self, report: EvaluationReport) -> None:
        """Log evaluation summary based on CRS type."""
        if isinstance(self.crs_executor, CRSPatchExecutor):
            verification_info = ""
            if report.patches_valid > 0 or report.patches_build_failed > 0:
                verification_info = (
                    f", {report.patches_valid} valid patches "
                    f"({report.patches_build_failed} build failed, "
                    f"{report.patches_pov_triggers} POV still triggers, "
                    f"{report.patches_test_failed} test failed)"
                )
            self.logger.info(
                f"Evaluation completed: {report.patches_generated} patches generated "
                f"from {report.total_input_povs} input POVs{verification_info}"
            )
        else:
            self.logger.info(
                f"Evaluation completed: {report.povs_found}/{report.total_povs} POVs detected "
                f"({report.success_rate:.1%} success rate)"
            )

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
    ) -> tuple[HarnessResult, list[VerifResult], list[PatchVerificationResult]]:
        """Run evaluation for a single harness with snapshot management and verification."""
        self.logger.info(f"Evaluating harness: {harness.name}")

        # Execute CRS with managers
        (
            harness_result,
            _,
            pov_verification_manager,
            patch_verification_manager,
        ) = self._execute_crs_with_managers(
            harness=harness,
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            trial_start_time=trial_start_time,
        )

        # Log POV verification manager final report if available (bug-finding CRS)
        if pov_verification_manager:
            report = pov_verification_manager.get_report()
            self.logger.info(
                f"POV verification report: "
                f"cpvs={len(report.cpvs_found)}/{report.total_expected_cpvs}, "
                f"povs={report.total_povs_processed}, "
                f"unintended_crashes={report.unintended_crashes}, "
                f"early_stopped={report.early_stopped}"
            )

        # Log patch verification manager final report if available (bug-fixing CRS)
        if patch_verification_manager:
            report = patch_verification_manager.get_report()
            self.logger.info(
                f"Patch discovery report: "
                f"patches={report.patches_total}/{report.input_cpvs_total}, "
                f"cpv_ids={report.cpvs_with_patches}"
            )

        # Run post-experiment coverage (only for bug-finding CRS with successful run)
        if (
            self.coverage_enabled
            and self.oss_fuzz_path
            and harness_result
            and harness_result.run_successful
            and isinstance(self.crs_executor, CRSBugFindingExecutor)
        ):
            self._run_post_experiment_coverage(
                benchmark_path=benchmark_path,
                trial_output_dir=trial_output_dir,
                harness_name=harness.name,
            )

        # Call verification phase callback
        if self.on_verification_start:
            self.on_verification_start()

        # POV verification: use manager's final sweep + drain (unified path)
        pov_verification_results: list[VerifResult] = []
        if (
            isinstance(self.crs_executor, CRSBugFindingExecutor)
            and pov_verification_manager
            and harness_result
            and harness_result.run_successful
            and not skip_verification
            and oss_fuzz_path
        ):
            # Final sweep: discover any remaining POVs written after last snapshot
            pov_verification_manager.on_snapshot(cycle=-1)
            # Wait for all async (Redis) verdicts to complete
            pov_verification_manager.drain_pending(
                per_pov_timeout=self.per_pov_verify_timeout,
                verify_timeout=self.verify_timeout,
            )
            # Export results in the same format as VerificationEngine
            pov_verification_results = (
                pov_verification_manager.get_verification_results()
            )

        # Patch verification: run separately (bug-fixing CRS only)
        patch_verification_results: list[PatchVerificationResult] = []
        if (
            isinstance(self.crs_executor, CRSPatchExecutor)
            and harness_result
            and harness_result.run_successful
            and not skip_verification
            and oss_fuzz_path
        ):
            patch_verification_results = self._verify_patches(
                benchmark_path=benchmark_path,
                trial_output_dir=trial_output_dir,
                oss_fuzz_path=oss_fuzz_path,
                harness_name=harness.name,
            )

        return harness_result, pov_verification_results, patch_verification_results

    def _execute_crs_with_managers(
        self,
        harness: HarnessFile,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
    ) -> tuple[
        HarnessResult,
        Any,
        Optional[POVVerificationManager],
        Optional[PatchVerificationManager],
    ]:
        """Execute CRS with coverage, snapshot, and verification managers.

        Returns:
            Tuple of (harness_result, coverage_manager, pov_verification_manager,
                     patch_verification_manager)
        """
        snapshot_manager = None
        snapshot_thread = None
        coverage_manager = None
        coverage_thread = None
        pov_verification_manager = None
        patch_verification_manager = None
        stop_event: Optional[threading.Event] = None
        harness_result = None

        try:
            # Start managers
            coverage_manager, coverage_thread, stop_event = (
                self._start_coverage_manager(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    trial_start_time=trial_start_time,
                    harness_name=harness.name,
                )
            )

            # Start POV verification manager for bug-finding CRS
            pov_verification_manager, pov_stop_event = (
                self._start_pov_verification_manager(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    trial_start_time=trial_start_time,
                    harness_name=harness.name,
                )
            )

            # Start patch verification manager for bug-fixing CRS
            patch_verification_manager = self._start_patch_verification_manager(
                trial_output_dir=trial_output_dir,
                trial_start_time=trial_start_time,
                harness_name=harness.name,
                benchmark_id=benchmark_path.name,
            )

            # Combine stop events: either coverage saturation OR all CPVs found can stop CRS
            if pov_stop_event and stop_event:
                # Both enabled: use POV stop event (it's more definitive)
                combined_stop_event = pov_stop_event
            elif pov_stop_event:
                combined_stop_event = pov_stop_event
            elif stop_event:
                combined_stop_event = stop_event
            else:
                combined_stop_event = None

            snapshot_manager, snapshot_thread = self._start_snapshot_manager(
                harness_name=harness.name,
                trial_output_dir=trial_output_dir,
                trial_start_time=trial_start_time,
                coverage_manager=coverage_manager,
                pov_verification_manager=pov_verification_manager,
                patch_verification_manager=patch_verification_manager,
            )

            # Create callback for run start
            def on_run_start() -> None:
                run_start = time.time()
                if snapshot_manager:
                    snapshot_manager.set_crs_run_start_time(run_start)
                if coverage_manager:
                    coverage_manager.collector.set_run_start_time(run_start)
                # Call external callback for job metadata tracking
                if self.on_run_start:
                    self.on_run_start()

            # Run CRS
            crs_result = self.crs_executor.run_crs(
                benchmark_path=benchmark_path,
                harness=harness,
                trial_output_dir=trial_output_dir,
                on_build_start=self.on_build_start,
                on_run_start=on_run_start,
                stop_event=combined_stop_event,
            )

            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=crs_result.execution_time,
                run_successful=crs_result.success,
                run_output=crs_result.output,
                build_time=crs_result.build_time,
                run_time=crs_result.run_time,
            )

        except Exception as e:
            self.logger.error(f"Failed to evaluate harness '{harness.name}': {str(e)}")
            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=0.0,
                run_successful=False,
                run_output=f"Error: {str(e)}",
            )

        finally:
            self._stop_managers(
                snapshot_manager=snapshot_manager,
                snapshot_thread=snapshot_thread,
                coverage_manager=coverage_manager,
                coverage_thread=coverage_thread,
                harness_name=harness.name,
            )

        return (
            harness_result,
            coverage_manager,
            pov_verification_manager,
            patch_verification_manager,
        )

    def _start_coverage_manager(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
    ) -> tuple[Any, Optional[threading.Thread], Optional[threading.Event]]:
        """Start coverage manager if enabled."""
        coverage_manager = None
        coverage_thread = None
        stop_event = None

        if not (self.coverage_enabled and self.oss_fuzz_path):
            return coverage_manager, coverage_thread, stop_event

        coverage_manager = self._create_coverage_manager(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            trial_start_time=trial_start_time,
            harness_name=harness_name,
        )

        if coverage_manager:
            coverage_thread = threading.Thread(target=coverage_manager.run, daemon=True)
            coverage_thread.start()
            self.logger.info("Coverage manager thread started")

            if self.coverage_early_stop:
                stop_event = threading.Event()
                saturation_thread = threading.Thread(
                    target=self._monitor_saturation,
                    args=(coverage_manager, stop_event),
                    daemon=True,
                )
                saturation_thread.start()
                self.logger.info("Saturation monitor thread started")

        return coverage_manager, coverage_thread, stop_event

    def _start_pov_verification_manager(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
    ) -> tuple[Optional[POVVerificationManager], Optional[threading.Event]]:
        """Start POV verification manager if enabled.

        Only creates manager for bug-finding CRS (CRSBugFindingExecutor).
        Bug-fixing CRS uses POVs as input, not output.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness

        Returns:
            Tuple of (POVVerificationManager, stop_event) or (None, None) if not applicable
        """
        # Only create POV verification manager for bug-finding CRS
        if not isinstance(self.crs_executor, CRSBugFindingExecutor):
            return None, None

        if not self.oss_fuzz_path:
            return None, None

        pov_verification_manager = self._create_pov_verification_manager(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            trial_start_time=trial_start_time,
            harness_name=harness_name,
        )

        stop_event = None
        if pov_verification_manager and self.pov_early_stop:
            stop_event = threading.Event()
            pov_verification_manager._stop_event = stop_event
            self.logger.info("POV verification early stop enabled")

        return pov_verification_manager, stop_event

    def _create_pov_verification_manager(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
    ) -> Optional[POVVerificationManager]:
        """Create POVVerificationManager for real-time POV verification during trial.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness

        Returns:
            POVVerificationManager instance or None if creation fails
        """
        if not self.oss_fuzz_path:
            self.logger.warning("POV verification enabled but oss_fuzz_path not set")
            return None

        try:
            import yaml

            from crsbench.validation.meta_adapter import MetaYamlAdapter

            # Load project.yaml for main_repo and language
            project_yaml = benchmark_path / "project.yaml"
            if not project_yaml.exists():
                self.logger.error(f"project.yaml not found: {project_yaml}")
                return None

            with project_yaml.open() as f:
                project_config = yaml.safe_load(f)

            main_repo = project_config.get("main_repo")
            repo_name = project_config.get("repo_name")
            language = project_config.get("language", "c")

            if not main_repo:
                self.logger.error(f"main_repo not found in {project_yaml}")
                return None

            # Load benchmark config via MetaYamlAdapter
            meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
            if not meta_yaml.exists():
                self.logger.error(f"meta.yaml not found: {meta_yaml}")
                return None

            try:
                adapter = MetaYamlAdapter.from_meta_yaml(
                    meta_yaml_path=meta_yaml,
                    benchmark_name=benchmark_path.name,
                    lang=language,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    repo_name=repo_name,
                )
            except (FileNotFoundError, ValueError) as e:
                self.logger.error(f"Failed to load meta.yaml: {e}")
                return None

            # Get total expected CPVs for this harness
            harness = adapter.get_harness(harness_name)
            if not harness or not harness.vulns:
                self.logger.warning(
                    f"No vulnerabilities found for harness '{harness_name}', "
                    "POV verification manager not created"
                )
                return None

            # Extract actual CPV IDs from harness vulnerabilities
            expected_cpv_ids = [vuln.vuln_keyword for vuln in harness.vulns]

            # Create POV verification config
            config = POVVerificationConfig(
                early_stop_enabled=self.pov_early_stop,
            )

            # Create verification engine
            engine = VerificationEngine(
                oss_fuzz_path=self.oss_fuzz_path,
                timeout=self.per_pov_verify_timeout,
                dedup_strategy=PatchBasedDedup(),
                build_workers=self.build_workers,
                verify_workers=self.verify_workers,
            )

            # POV output directory (where CRS writes discovered POVs)
            pov_output_dir = trial_output_dir / "output" / "povs"

            # Create POV verification manager
            # Derive trial_id from output dir name for async result correlation
            trial_id = trial_output_dir.name

            manager = POVVerificationManager(
                trial_dir=trial_output_dir,
                pov_output_dir=pov_output_dir,
                config=config,
                harness_name=harness_name,
                benchmark_id=benchmark_path.name,
                expected_cpv_ids=expected_cpv_ids,
                trial_start_time=trial_start_time,
                engine=engine,
                adapter=adapter,
                redis_host=self.redis_host,
                experiment_name=self.experiment_name,
                trial_id=trial_id,
            )

            self.logger.info(
                f"POV verification manager created: harness={harness_name}, "
                f"expected_cpvs={len(expected_cpv_ids)}, early_stop={self.pov_early_stop}"
            )

            return manager

        except Exception as e:
            self.logger.error(
                f"Failed to create POV verification manager: {e}", exc_info=True
            )
            return None

    def _start_patch_verification_manager(
        self,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
        benchmark_id: str,
    ) -> Optional[PatchVerificationManager]:
        """Start patch verification manager for bug-fixing CRS.

        Only creates manager for bug-fixing CRS (CRSPatchExecutor).
        Bug-finding CRS uses POVVerificationManager instead.

        Args:
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness
            benchmark_id: Benchmark identifier

        Returns:
            PatchVerificationManager or None if not applicable
        """
        # Only create patch verification manager for bug-fixing CRS
        if not isinstance(self.crs_executor, CRSPatchExecutor):
            return None

        try:
            # Patch output directory (where CRS writes patches)
            patch_output_dir = trial_output_dir / "output" / "patches"

            # Get input CPVs count (patches are generated per CPV, not per POV)
            # CPVs are provided as input to bug-fixing CRS
            cpvs_dir = trial_output_dir / "crs-input" / "cpvs"
            input_cpvs_total = len(list(cpvs_dir.iterdir())) if cpvs_dir.exists() else 0

            manager = PatchVerificationManager(
                trial_dir=trial_output_dir,
                patch_output_dir=patch_output_dir,
                harness_name=harness_name,
                benchmark_id=benchmark_id,
                input_cpvs_total=input_cpvs_total,
                trial_start_time=trial_start_time,
            )

            self.logger.info(
                f"Patch verification manager created: harness={harness_name}, "
                f"input_cpvs={input_cpvs_total}"
            )

            return manager

        except Exception as e:
            self.logger.error(
                f"Failed to create patch verification manager: {e}", exc_info=True
            )
            return None

    def _start_snapshot_manager(
        self,
        harness_name: str,
        trial_output_dir: Path,
        trial_start_time: float,
        coverage_manager: Any,
        pov_verification_manager: Optional[POVVerificationManager] = None,
        patch_verification_manager: Optional[PatchVerificationManager] = None,
    ) -> tuple[Optional[SnapshotManager], Optional[threading.Thread]]:
        """Start snapshot manager if enabled."""
        if not (self.snapshot_period and self.snapshot_period > 0):
            return None, None

        self.logger.info(
            f"Starting snapshot manager for harness '{harness_name}' "
            f"(period={self.snapshot_period}s)"
        )

        snapshot_manager = SnapshotManager(
            trial_dir=trial_output_dir,
            snapshot_period=self.snapshot_period,
            trial_start_time=trial_start_time,
            coverage_manager=coverage_manager,
            pov_verification_manager=pov_verification_manager,
            patch_verification_manager=patch_verification_manager,
            llm_tracker=self.llm_tracker,
            llm_api_key=self.llm_api_key,
            llm_trial_id=self.llm_trial_id,
        )
        snapshot_thread = threading.Thread(target=snapshot_manager.run, daemon=True)
        snapshot_thread.start()

        return snapshot_manager, snapshot_thread

    def _stop_managers(
        self,
        snapshot_manager: Optional[SnapshotManager],
        snapshot_thread: Optional[threading.Thread],
        coverage_manager: Any,
        coverage_thread: Optional[threading.Thread],
        harness_name: str,
    ) -> None:
        """Stop snapshot and coverage managers."""
        if snapshot_manager:
            self.logger.info(
                f"Capturing final snapshot for harness '{harness_name}'..."
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
                    self.logger.warning("Snapshot thread did not stop within timeout")

        if coverage_manager:
            self.logger.info("Stopping coverage manager...")
            coverage_manager.stop()
            if coverage_thread and coverage_thread.is_alive():
                coverage_thread.join(timeout=5.0)
                if coverage_thread.is_alive():
                    self.logger.warning("Coverage thread did not stop within timeout")

    def _verify_patches(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        oss_fuzz_path: Path,
        harness_name: str,
    ) -> list[PatchVerificationResult]:
        """Verify CRS-generated patches for a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Path to trial output directory containing patches
            oss_fuzz_path: Path to oss-fuzz directory
            harness_name: Name of the harness

        Returns:
            List of patch verification results
        """
        try:
            self.logger.info(f"Starting patch verification for harness: {harness_name}")

            # Patches are in trial_output_dir/output/patches/
            patch_dir = trial_output_dir / "output" / "patches"
            if not patch_dir.exists():
                self.logger.warning(f"No patches directory found: {patch_dir}")
                return []

            # POVs were prepared in trial_output_dir/crs-input/povs/
            pov_dir = trial_output_dir / "crs-input" / "povs"
            if not pov_dir.exists():
                self.logger.warning(f"No POVs directory found: {pov_dir}")
                return []

            # Create verification engine with trial-specific work directory
            work_dir = trial_output_dir / "patch-verify"
            work_dir.mkdir(parents=True, exist_ok=True)

            engine = PatchVerificationEngine(
                oss_fuzz_path=oss_fuzz_path,
                timeout=self.per_pov_verify_timeout,
                build_timeout=1200,
                test_timeout=1800,
                work_dir=work_dir,
                force_rebuild=True,  # Always rebuild for fresh verification
                build_workers=self.build_workers,
                verify_workers=self.verify_workers,
            )

            # Run verification
            results = engine.verify_patches(
                benchmark_path=benchmark_path,
                patch_dir=patch_dir,
                harness=harness_name,
                pov_dir=pov_dir,
            )

            # Log summary
            valid_count = sum(1 for r in results if r.is_valid)
            self.logger.info(
                f"Patch verification completed: {valid_count}/{len(results)} patches valid"
            )

            # Cleanup temporary files
            engine.cleanup()

            return results

        except Exception as e:
            self.logger.error(
                f"Patch verification failed for harness '{harness_name}': {e}",
                exc_info=True,
            )
            return []

    def _create_coverage_manager(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
    ):
        """Create CoverageManager for coverage collection during trial.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness

        Returns:
            CoverageManager instance or None if creation fails
        """
        if not self.oss_fuzz_path:
            self.logger.warning("Coverage enabled but oss_fuzz_path not set")
            return None

        try:
            import yaml

            from crsbench.builder import BuildConfig, OSSFuzzBuilder, VariantType
            from crsbench.evaluation.coverage import CoverageManager
            from crsbench.evaluation.coverage.collector import CoverageCollector
            from crsbench.evaluation.coverage.models import CoverageConfig
            from crsbench.evaluation.coverage.store import CoverageStore
            from crsbench.evaluation.coverage.strategy import create_coverage_strategy
            from crsbench.validation.meta_adapter import MetaYamlAdapter

            # Load project.yaml for main_repo and language
            project_yaml = benchmark_path / "project.yaml"
            if not project_yaml.exists():
                self.logger.error(f"project.yaml not found: {project_yaml}")
                return None

            with project_yaml.open() as f:
                project_config = yaml.safe_load(f)

            main_repo = project_config.get("main_repo")
            repo_name = project_config.get("repo_name")
            language = project_config.get("language", "c")

            if not main_repo:
                self.logger.error(f"main_repo not found in {project_yaml}")
                return None

            # Load benchmark config via MetaYamlAdapter
            meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
            if not meta_yaml.exists():
                self.logger.error(f"meta.yaml not found: {meta_yaml}")
                return None

            try:
                adapter = MetaYamlAdapter.from_meta_yaml(
                    meta_yaml_path=meta_yaml,
                    benchmark_name=benchmark_path.name,
                    lang=language,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    repo_name=repo_name,
                )
            except (FileNotFoundError, ValueError) as e:
                self.logger.error(f"Failed to load meta.yaml: {e}")
                return None

            # Get mode and commit from adapter
            mode = adapter.get_mode()
            target_commit = adapter.get_ref_commit() or adapter.get_base_commit()

            # Get coverage variant name (uses adapter's mode)
            coverage_variant = adapter.get_variant_name(VariantType.COVERAGE)

            # Build coverage variant if not already built
            builder = OSSFuzzBuilder(self.oss_fuzz_path)
            if not builder.is_variant_built(coverage_variant):
                self.logger.info(f"Building coverage variant: {coverage_variant}")

                config = BuildConfig(
                    benchmark_name=benchmark_path.name,
                    variant_type=VariantType.COVERAGE,
                    commit=target_commit,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    mode=mode,
                    language=language,
                    repo_name=repo_name,
                )
                build_result = builder.build_single(config)
                if not build_result.success:
                    self.logger.error(
                        f"Failed to build coverage variant: {coverage_variant}"
                    )
                    return None

            # Create coverage config
            coverage_config = CoverageConfig(
                enabled=True,
                language=language,
                metric="line",
                saturation_time=self.coverage_saturation_time,
            )

            # Create coverage strategy
            strategy = create_coverage_strategy(
                oss_fuzz_path=self.oss_fuzz_path,
                project_name=coverage_variant,
                language=language,
            )

            # Create coverage store
            coverage_store_dir = trial_output_dir / "coverage"
            store = CoverageStore(coverage_store_dir)

            # Create collector with harness_name
            # Pass output_dir so coverage files are saved to trial-N/coverage/
            # Pass trial_start_time so we can calculate elapsed_time for corpus files
            collector = CoverageCollector(
                strategy,
                store,
                harness_name,
                output_dir=coverage_store_dir,
                trial_start_time=trial_start_time,
            )

            # Corpus directory (where CRS puts corpus files)
            # Use symlink path - don't create directory as it blocks the symlink
            # The "output" symlink is created by CRS run pointing to artifacts
            corpus_dir = trial_output_dir / "output" / "corpus"

            # Create manager
            manager = CoverageManager(
                trial_dir=trial_output_dir,
                collector=collector,
                config=coverage_config,
                harness_name=harness_name,
                corpus_dir=corpus_dir,
                trial_start_time=trial_start_time,
                store=store,
            )

            self.logger.info(
                f"Coverage manager created for {harness_name} (language={language})"
            )
            return manager

        except Exception as e:
            self.logger.error(f"Failed to create coverage manager: {e}", exc_info=True)
            return None

    def _run_post_experiment_coverage(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        harness_name: str,
    ) -> None:
        """Run coverage collection on final corpus after experiment completes.

        This provides a final accurate coverage measurement using all corpus
        files generated during the trial.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            harness_name: Name of the harness
        """
        if not self.oss_fuzz_path:
            return

        corpus_dir = trial_output_dir / "output" / "corpus"
        if not corpus_dir.exists() or not any(corpus_dir.iterdir()):
            self.logger.info("No corpus files for post-experiment coverage")
            return

        try:
            import yaml

            from crsbench.builder import VariantType
            from crsbench.evaluation.coverage.models import CoverageSummary
            from crsbench.evaluation.coverage.strategy import (
                create_coverage_strategy,
                parse_llvm_cov_summary,
            )
            from crsbench.validation.meta_adapter import MetaYamlAdapter

            # Load project.yaml for language and main_repo
            project_yaml = benchmark_path / "project.yaml"
            language = "c"
            main_repo = ""
            repo_name = None
            if project_yaml.exists():
                with project_yaml.open() as f:
                    project_config = yaml.safe_load(f)
                    language = project_config.get("language", "c")
                    main_repo = project_config.get("main_repo", "")
                    repo_name = project_config.get("repo_name")

            # Load benchmark config via MetaYamlAdapter
            meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
            if not meta_yaml.exists():
                self.logger.error(f"meta.yaml not found: {meta_yaml}")
                return

            try:
                adapter = MetaYamlAdapter.from_meta_yaml(
                    meta_yaml_path=meta_yaml,
                    benchmark_name=benchmark_path.name,
                    lang=language,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    repo_name=repo_name,
                )
            except (FileNotFoundError, ValueError) as e:
                self.logger.error(f"Failed to load meta.yaml: {e}")
                return

            # Get coverage variant name (uses adapter's mode)
            coverage_variant = adapter.get_variant_name(VariantType.COVERAGE)

            self.logger.info(
                f"Running post-experiment coverage on {corpus_dir} "
                f"({len(list(corpus_dir.iterdir()))} files)"
            )

            # Create strategy and collect coverage
            strategy = create_coverage_strategy(
                oss_fuzz_path=self.oss_fuzz_path,
                project_name=coverage_variant,
                language=language,
            )

            summary_path = strategy.collect_batch_coverage(
                harness_path=Path(harness_name),
                corpus_dir=corpus_dir,
            )

            # Parse and save results
            cov_stats = parse_llvm_cov_summary(summary_path)
            summary = CoverageSummary(
                metric="line",
                corpus_total=len(list(corpus_dir.iterdir())),
                corpus_contributing=len(list(corpus_dir.iterdir())),
                lines_covered=int(cov_stats.get("lines_covered", 0)),
                lines_total=int(cov_stats.get("lines_total", 0)),
                lines_percent=float(cov_stats.get("lines_percent", 0.0)),
                functions_covered=int(cov_stats.get("functions_covered", 0)),
                functions_total=int(cov_stats.get("functions_total", 0)),
            )

            # Save final coverage report
            import json

            final_coverage_path = trial_output_dir / "final_coverage.json"
            final_coverage_path.write_text(
                json.dumps(
                    {
                        "harness": harness_name,
                        "summary": summary.model_dump(),
                    },
                    indent=2,
                )
            )

            self.logger.info(
                f"Post-experiment coverage: {summary.lines_covered}/{summary.lines_total} "
                f"lines ({summary.lines_percent:.1f}%)"
            )

        except Exception as e:
            self.logger.error(f"Post-experiment coverage failed: {e}", exc_info=True)

    def _monitor_saturation(
        self,
        coverage_manager,
        stop_event: threading.Event,
    ) -> None:
        """Monitor coverage saturation and signal early stop when detected.

        This method runs in a separate thread and periodically checks if
        coverage has saturated. When saturation is detected, it signals
        the stop_event to terminate the CRS process early.

        Args:
            coverage_manager: CoverageManager instance to monitor
            stop_event: Event to signal when saturation is detected
        """
        check_interval = 5.0  # Check every 5 seconds

        self.logger.debug("Saturation monitor: started")

        while not stop_event.is_set():
            # Check if saturation is detected
            if coverage_manager.is_saturated():
                self.logger.info(
                    "Coverage saturation detected by monitor - signaling early stop"
                )
                stop_event.set()
                break

            # Wait briefly before next check
            time.sleep(check_interval)

        self.logger.debug("Saturation monitor: stopped")

    def _save_patch_verification_results(
        self,
        trial_output_dir: Path,
        results: list[PatchVerificationResult],
        total_input_povs: int,
    ) -> None:
        """Save patch verification results to trial output directory.

        Args:
            trial_output_dir: Trial output directory
            results: List of patch verification results
            total_input_povs: Number of input POVs
        """
        output = PatchVerificationOutput.from_results(results, total_input_povs)

        results_path = trial_output_dir / "patch_verification_results.json"
        results_path.write_text(output.model_dump_json(indent=2))

        self.logger.info(
            f"Saved patch verification results to {results_path} "
            f"({output.summary.valid}/{output.summary.patches_generated} valid)"
        )
