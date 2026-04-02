"""Main benchmark runner for CRS evaluation."""

import hashlib
import re
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.trial_identity import build_trial_uid
from crsbench.evaluation.trial_paths import count_visible_files

if TYPE_CHECKING:
    from crsbench.evaluation.litellm_tracker import LiteLLMTracker
from crsbench.evaluation.results import (
    EvaluationReport,
    HarnessResult,
    ResultCollector,
)
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.evaluation.verification import PovVerificationResult as VerifResult
from crsbench.evaluation.verification import VerificationEngine
from crsbench.evaluation.verification.dedup import get_dedup_strategy
from crsbench.evaluation.verification.models import (
    PatchInfo,
    PatchVerificationOutput,
    PatchVerificationResult,
    PatchVerificationStatus,
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


class PatchDiscoveryError(Exception):
    """Raised when trial patch layout is invalid for verification."""


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
        adapter: OssCrsAdapter,
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
        llm_accounting_settle_seconds: int = 60,
        max_pov_variants_per_cpv: Optional[int] = 1,
        patch_verify_variants: bool = False,
        pov_input_enabled: bool = False,
        sarif_input_enabled: bool = False,
        sarif_level: Optional[int] = None,
        seed_corpus_enabled: bool = False,
        seed_corpus_max_time: Optional[int] = None,
        diff_input_enabled: bool = False,
        redis_host: Optional[str] = None,
        experiment_name: Optional[str] = None,
        pov_dedup_strategy: str = "patch-based",
        inc_image_policy: Optional[str] = None,
        inc_image_registry: Optional[str] = None,
        inc_image_max_pull_bytes: Optional[int] = None,
        inc_image_pull_timeout: Optional[int] = None,
        local_image_prefix: Optional[str] = None,
    ):
        """Initialize benchmark runner.

        Args:
            adapter: CRS adapter for running evaluation (required).
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
            llm_accounting_settle_seconds: Minimum time to wait after CRS run end
                before final LLM usage/log capture. Remaining wait is computed after
                manager shutdown work; set to 0 to disable.
            max_pov_variants_per_cpv: Max POV variants per CPV for bug-fixing input
                staging.
                1 = single POV per CPV (default), N = multiple, None = all.
            patch_verify_variants: For bug-fixing patch verification, verify
                patches against all benchmark POV variants per CPV. When False
                (default), verify against a single POV (pov_0-like behavior).
            pov_input_enabled: Whether to stage/provide explicit bug-fixing POV inputs.
            sarif_input_enabled: Whether to stage/provide bug-candidate SARIF inputs.
            sarif_level: SARIF hint level to stage when sarif_input_enabled is true.
            seed_corpus_enabled: Whether to stage/provide seed corpus input.
            seed_corpus_max_time: Optional max relative time for staged seed corpus files.
            diff_input_enabled: Whether to stage/provide ``.aixcc/ref.diff`` as runtime diff input.
            redis_host: Redis server hostname for async POV verification
            experiment_name: Experiment name for async verify queue naming
            pov_dedup_strategy: POV deduplication strategy name
        """
        self.adapter = adapter
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
        if llm_accounting_settle_seconds < 0:
            raise ValueError(
                "llm_accounting_settle_seconds must be >= 0, "
                f"got {llm_accounting_settle_seconds}"
            )
        self.llm_accounting_settle_seconds = llm_accounting_settle_seconds
        self.max_pov_variants_per_cpv = max_pov_variants_per_cpv
        self.patch_verify_variants = patch_verify_variants
        self.pov_input_enabled = pov_input_enabled
        self.sarif_input_enabled = sarif_input_enabled
        self.sarif_level = sarif_level
        self.seed_corpus_enabled = seed_corpus_enabled
        self.seed_corpus_max_time = seed_corpus_max_time
        self.diff_input_enabled = diff_input_enabled
        self.redis_host = redis_host
        self.experiment_name = experiment_name
        self.pov_dedup_strategy = pov_dedup_strategy
        self.inc_image_policy = inc_image_policy
        self.inc_image_registry = inc_image_registry
        self.inc_image_max_pull_bytes = inc_image_max_pull_bytes
        self.inc_image_pull_timeout = inc_image_pull_timeout
        self.local_image_prefix = local_image_prefix
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

    @property
    def _crs_type(self) -> str:
        """Return CRS type from adapter mode ('bug-finding' or 'bug-fixing')."""
        return self.adapter.mode

    def run_benchmark(
        self,
        benchmark_harness: "BenchmarkHarness",
        mode: Optional[str] = None,
        crs_config: Optional[dict[str, Any]] = None,
        trial_output_dir: Optional[Path] = None,
        oss_fuzz_path: Optional[Path] = None,
        *,
        skip_verification: bool = False,
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
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
                    sanitizer=sanitizer,
                    target_cpv_id=target_cpv_id,
                )
            )
            collector.add_harness_result(harness_result)

            # Result collection phase
            self._collect_crs_results(
                collector=collector,
                trial_output_dir=trial_output_dir,
                pov_verification_results=pov_verification_results,
                patch_verification_results=patch_verification_results,
                target_cpv_id=target_cpv_id,
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
        if trial_output_dir:
            self.logger.info("Pre-building CRS before snapshot period...")
            if self.on_build_start:
                self.on_build_start()
            self.adapter.build(benchmark_path, trial_output_dir)

    def _collect_crs_results(
        self,
        collector: ResultCollector,
        trial_output_dir: Optional[Path],
        pov_verification_results: list[VerifResult],
        patch_verification_results: list[PatchVerificationResult],
        *,
        target_cpv_id: str | None = None,
    ) -> None:
        """Collect results based on CRS type.

        Similar to how POV stats are derived from verification results,
        patch stats are also derived from verification results.
        """
        if self._crs_type == "bug-finding":
            if pov_verification_results:
                collector.set_pov_stats(pov_verification_results)

        elif self._crs_type == "bug-fixing" and trial_output_dir:
            # Always set input POV count even when no patch results were produced.
            # This keeps reporting accurate for "0 patches from N input POVs".
            povs_dir = trial_output_dir / "crs-input" / "povs"
            total_input_povs = count_visible_files(povs_dir)
            patch_dir = trial_output_dir / "output" / "patches"
            produced_patches = len(
                self._discover_trial_patches_for_verification(
                    patch_dir=patch_dir, target_cpv_id=target_cpv_id
                )
            )
            collector.set_patch_stats(
                total_input_povs,
                produced_patches,
                patch_verification_results,
            )
            self.logger.info(
                f"Patch reporting: produced={produced_patches}, "
                f"verified={len(patch_verification_results)} from "
                f"{total_input_povs} input POVs"
            )
            if patch_verification_results:
                self._save_patch_verification_results(
                    trial_output_dir, patch_verification_results, total_input_povs
                )

    def _log_evaluation_summary(self, report: EvaluationReport) -> None:
        """Log evaluation summary based on CRS type."""
        if self._crs_type == "bug-fixing":
            verification_info = ""
            if report.patches_verified > 0:
                verification_info = (
                    f", {report.patches_valid} valid patches "
                    f"({report.patches_build_failed} build failed, "
                    f"{report.patches_pov_triggers} POV still triggers, "
                    f"{report.patches_test_failed} test failed)"
                )
            self.logger.info(
                f"Evaluation completed: {report.patches_generated} patches produced, "
                f"{report.patches_verified} verified "
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
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
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
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
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
            and harness_result
            and harness_result.run_successful
            and self._crs_type == "bug-finding"
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
        # Always verify regardless of run exit code — oss-crs run may
        # return non-zero (e.g. fuzzer killed by timeout) while POVs exist.
        pov_verification_results: list[VerifResult] = []
        if (
            self._crs_type == "bug-finding"
            and pov_verification_manager
            and harness_result
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
        # Always verify regardless of run exit code (same rationale as POV).
        patch_verification_results: list[PatchVerificationResult] = []
        if (
            self._crs_type == "bug-fixing"
            and harness_result
            and not skip_verification
            and oss_fuzz_path
        ):
            patch_verification_results = self._verify_patches(
                benchmark_path=benchmark_path,
                trial_output_dir=trial_output_dir,
                oss_fuzz_path=oss_fuzz_path,
                harness_name=harness.name,
                sanitizer=sanitizer,
                target_cpv_id=target_cpv_id,
            )

        return harness_result, pov_verification_results, patch_verification_results

    def _execute_crs_with_managers(
        self,
        harness: HarnessFile,
        benchmark_path: Path,
        trial_output_dir: Path,
        trial_start_time: float,
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
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
        cleanup_issue: Optional[str] = None
        crs_run_started = False
        crs_run_returned = False
        crs_run_end_monotonic: Optional[float] = None

        try:
            # Pre-resolve artifact paths so exchange_dir is available
            # before verification managers are created.
            self.adapter.resolve_artifacts(
                benchmark_path, harness.name, trial_output_dir
            )

            # Stage configured runtime inputs from benchmark .aixcc into
            # the trial directory before managers are initialized.
            self._prepare_runtime_inputs(
                benchmark_path=benchmark_path,
                harness_name=harness.name,
                trial_output_dir=trial_output_dir,
                target_cpv_id=target_cpv_id,
            )

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
                    sanitizer=sanitizer,
                )
            )

            # Start patch verification manager for bug-fixing CRS
            patch_verification_manager = self._start_patch_verification_manager(
                trial_output_dir=trial_output_dir,
                trial_start_time=trial_start_time,
                harness_name=harness.name,
                benchmark_id=benchmark_path.name,
                target_cpv_id=target_cpv_id,
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
                nonlocal crs_run_started
                crs_run_started = True
                run_start = time.time()
                if snapshot_manager:
                    snapshot_manager.set_crs_run_start_time(run_start)
                if coverage_manager:
                    coverage_manager.collector.set_run_start_time(run_start)
                if pov_verification_manager:
                    pov_verification_manager.set_crs_run_start_time(run_start)
                # Call external callback for job metadata tracking
                if self.on_run_start:
                    self.on_run_start()

            # Run CRS
            try:
                crs_result = self.adapter.run(
                    benchmark_path=benchmark_path,
                    harness=harness,
                    trial_output_dir=trial_output_dir,
                    on_build_start=self.on_build_start,
                    on_run_start=on_run_start,
                    stop_event=combined_stop_event,
                )
                crs_run_returned = True
            finally:
                if crs_run_started and crs_run_returned:
                    # Use monotonic clock for duration/remaining-wait math.
                    crs_run_end_monotonic = time.monotonic()

            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=crs_result.execution_time,
                run_successful=crs_result.success,
                run_output=crs_result.output,
                build_time=crs_result.build_time,
                run_time=crs_result.run_time,
            )

            # Collect adapter results (copies SUBMIT_DIR artifacts to trial_output_dir/output/)
            # Always collect regardless of exit code — oss-crs run returns
            # non-zero when Docker containers exit non-zero (e.g. fuzzer killed
            # by timeout), but POVs/patches may still be present in SUBMIT_DIR.
            try:
                collect_meta = self.adapter.collect_results(
                    trial_output_dir, harness.name
                )
                self.logger.info(f"Adapter collect_results: {collect_meta}")
            except Exception as collect_err:
                self.logger.warning(
                    "Failed to collect adapter results: {}", collect_err
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
            cleanup_issue = self._stop_managers(
                snapshot_manager=snapshot_manager,
                snapshot_thread=snapshot_thread,
                coverage_manager=coverage_manager,
                coverage_thread=coverage_thread,
                harness_name=harness.name,
                crs_run_end_monotonic=crs_run_end_monotonic,
            )

        if cleanup_issue:
            if harness_result is None:
                harness_result = HarnessResult(
                    name=harness.name,
                    path=harness.path,
                    execution_time=0.0,
                    run_successful=False,
                    run_output=f"Cleanup error: {cleanup_issue}",
                )
            else:
                current_output = harness_result.run_output or ""
                separator = "\n" if current_output else ""
                harness_result.run_output = (
                    f"{current_output}{separator}[cleanup] {cleanup_issue}"
                )

        return (
            harness_result,
            coverage_manager,
            pov_verification_manager,
            patch_verification_manager,
        )

    def _prepare_runtime_inputs(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path,
        target_cpv_id: str | None = None,
    ) -> None:
        """Stage configured runtime inputs based on effective input settings."""
        # CPV-targeted POV staging is meaningful only for bug-fixing runs.
        pov_target_cpv_id = target_cpv_id if self._crs_type == "bug-fixing" else None

        if self.pov_input_enabled and self._crs_type != "bug-finding":
            self._prepare_bugfix_inputs(
                benchmark_path=benchmark_path,
                harness_name=harness_name,
                trial_output_dir=trial_output_dir,
                target_cpv_id=pov_target_cpv_id,
            )
        elif self.pov_input_enabled and self._crs_type == "bug-finding":
            self.logger.warning(
                "pov_input_enabled is set but CRS type is bug-finding; "
                "skipping POV input staging to avoid leaking ground-truth answers"
            )
        else:
            for stale in [
                trial_output_dir / "povs",
                trial_output_dir / "crs-input" / "povs",
                trial_output_dir / "crs-input" / "cpvs",
            ]:
                if stale.is_dir():
                    shutil.rmtree(stale)
            self.logger.info("Runtime POV input staging disabled")

        self._prepare_seed_corpus_inputs(
            benchmark_path=benchmark_path,
            harness_name=harness_name,
            trial_output_dir=trial_output_dir,
        )
        self._prepare_bug_candidate_inputs(
            benchmark_path=benchmark_path,
            harness_name=harness_name,
            trial_output_dir=trial_output_dir,
            target_cpv_id=target_cpv_id,
        )
        self._prepare_ref_diff_input(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
        )

    def _prepare_bug_candidate_inputs(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path,
        target_cpv_id: str | None = None,
    ) -> None:
        """Stage SARIF bug-candidate inputs into ``trial/bug-candidates`` when enabled."""
        target_dir = trial_output_dir / "bug-candidates"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        if not self.sarif_input_enabled or self.sarif_level is None:
            return

        from crsbench.validation.meta_adapter import MetaYamlAdapter

        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if adapter is None:
            raise EvaluationError(
                "SARIF bug-candidate input enabled but metadata could not be loaded: "
                f"{benchmark_path}"
            )

        harness = adapter.get_harness(harness_name)
        if harness is None or not harness.vulns:
            self.logger.warning(
                "SARIF bug-candidate input enabled but no CPVs available: "
                f"harness={harness_name}; skipping SARIF staging"
            )
            return

        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for vuln in harness.vulns:
            cpv_id = vuln.vuln_keyword
            if target_cpv_id is not None and cpv_id != target_cpv_id:
                continue

            sarif_path = (
                benchmark_path
                / ".aixcc"
                / harness_name
                / cpv_id
                / "hints"
                / f"level_{self.sarif_level}.sarif"
            )
            if not sarif_path.exists():
                continue

            dest = target_dir / f"{cpv_id}.sarif"
            shutil.copy2(sarif_path, dest)
            copied += 1

        if copied == 0 and target_dir.exists():
            shutil.rmtree(target_dir)
            raise EvaluationError(
                "SARIF bug-candidate input enabled but no matching files found: "
                f"harness={harness_name}, level={self.sarif_level}"
            )

        self.logger.info(
            "Prepared bug-candidate inputs: "
            f"harness={harness_name}, sarif_files={copied}, level={self.sarif_level}"
        )

    def _prepare_bugfix_inputs(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path,
        target_cpv_id: str | None = None,
    ) -> None:
        """Prepare bug-fixing input CPVs/POVs in trial-local directories.

        Expected benchmark layout:
        ``<benchmark>/.aixcc/<harness>/cpv_*/blobs/*.blob``

        Created trial layout:
        - ``trial/crs-input/cpvs/<cpv_id>/`` (marker dirs for CPV count/tracking)
        - ``trial/crs-input/povs/<name>`` (input POV blobs)
        - ``trial/povs/<name>`` (adapter input path for oss-crs run)
          where ``name`` is ``<cpv_id>`` for first variant and
          ``<cpv_id>__<pov_id>`` for additional variants.
        """
        from crsbench.validation.meta_adapter import MetaYamlAdapter

        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if adapter is None:
            self.logger.warning(
                f"Failed to load MetaYamlAdapter for bug-fixing input prep: {benchmark_path}"
            )
            return

        harness = adapter.get_harness(harness_name)
        if harness is None or not harness.vulns:
            self.logger.warning(
                f"No vulnerabilities found for harness during bug-fixing input prep: {harness_name}"
            )
            return

        cpvs_dir = trial_output_dir / "crs-input" / "cpvs"
        input_povs_dir = trial_output_dir / "crs-input" / "povs"
        adapter_povs_dir = trial_output_dir / "povs"
        for stale_dir in (cpvs_dir, input_povs_dir, adapter_povs_dir):
            if stale_dir.exists():
                shutil.rmtree(stale_dir)
        cpvs_dir.mkdir(parents=True, exist_ok=True)
        input_povs_dir.mkdir(parents=True, exist_ok=True)
        adapter_povs_dir.mkdir(parents=True, exist_ok=True)

        staged_cpvs = 0
        staged_povs = 0
        seen_cpvs: set[str] = set()
        for vuln in harness.vulns:
            cpv_id = vuln.vuln_keyword
            if cpv_id in seen_cpvs:
                continue
            seen_cpvs.add(cpv_id)

            if target_cpv_id is not None and cpv_id != target_cpv_id:
                continue

            if not vuln.povs:
                continue

            available: list[tuple[str, Path]] = []
            for pov in vuln.povs:
                src_blob = adapter.get_pov_path(harness_name, cpv_id, pov.id)
                if src_blob is None or not src_blob.exists():
                    self.logger.warning(
                        f"Missing POV blob for bug-fixing input prep: "
                        f"harness={harness_name}, cpv={cpv_id}, pov={pov.id}"
                    )
                    continue
                available.append((pov.id, src_blob))

            if not available:
                continue

            # Track CPV identity for manager counts.
            (cpvs_dir / cpv_id).mkdir(parents=True, exist_ok=True)
            staged_cpvs += 1

            selected = (
                available
                if self.max_pov_variants_per_cpv is None
                else available[: self.max_pov_variants_per_cpv]
            )
            for i, (pov_id, src_blob) in enumerate(selected):
                name = cpv_id if i == 0 else f"{cpv_id}__{pov_id}"
                shutil.copy2(src_blob, input_povs_dir / name)
                shutil.copy2(src_blob, adapter_povs_dir / name)
                staged_povs += 1

        if target_cpv_id is not None and staged_cpvs == 0:
            raise EvaluationError(
                f"Target CPV not found for harness '{harness_name}': {target_cpv_id}"
            )

        self.logger.info(
            "Prepared bug-fixing trial inputs: "
            f"harness={harness_name}, cpvs={staged_cpvs}, pov_files={staged_povs}, "
            "max_pov_variants_per_cpv="
            f"{self.max_pov_variants_per_cpv if self.max_pov_variants_per_cpv is not None else 'all'}"
        )

    def _prepare_seed_corpus_inputs(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path,
    ) -> None:
        """Stage benchmark seed corpus into ``trial/seeds`` when enabled."""
        target_dir = trial_output_dir / "seeds"
        if not self.seed_corpus_enabled:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            return

        from crsbench.benchmark.seed import SeedCorpusPreparer

        preparer = SeedCorpusPreparer(benchmark_path, harness_name)
        if not preparer.has_seed_corpus():
            if target_dir.exists():
                shutil.rmtree(target_dir)
            raise EvaluationError(
                f"Seed corpus input enabled but unavailable: harness={harness_name}"
            )

        result = preparer.prepare(
            target_dir,
            max_time=self.seed_corpus_max_time,
            force=True,
        )
        self.logger.info(
            "Prepared seed corpus input: "
            f"harness={harness_name}, copied={result.copied_files}/{result.total_files}, "
            f"max_time={self.seed_corpus_max_time}"
        )

    def _prepare_ref_diff_input(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
    ) -> None:
        """Stage ``.aixcc/ref.diff`` into ``trial/ref.diff`` when enabled."""
        target = trial_output_dir / "ref.diff"
        if not self.diff_input_enabled:
            if target.exists():
                target.unlink()
            return

        source = benchmark_path / ".aixcc" / "ref.diff"
        if not source.exists():
            if target.exists():
                target.unlink()
            raise EvaluationError(
                f"Diff input enabled but benchmark ref.diff is missing: {source}"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.logger.info(f"Prepared diff input at: {target}")

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
        sanitizer: str = "address",
    ) -> tuple[Optional[POVVerificationManager], Optional[threading.Event]]:
        """Start POV verification manager if enabled.

        Only creates manager for bug-finding CRS.
        Bug-fixing CRS uses POVs as input, not output.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness
            sanitizer: Trial sanitizer for scoped async build selection

        Returns:
            Tuple of (POVVerificationManager, stop_event) or (None, None) if not applicable
        """
        # Only create POV verification manager for bug-finding CRS
        if self._crs_type != "bug-finding":
            return None, None

        if not self.oss_fuzz_path:
            return None, None

        pov_verification_manager = self._create_pov_verification_manager(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            trial_start_time=trial_start_time,
            harness_name=harness_name,
            sanitizer=sanitizer,
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
        sanitizer: str = "address",
    ) -> Optional[POVVerificationManager]:
        """Create POVVerificationManager for real-time POV verification during trial.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial output directory
            trial_start_time: Trial start timestamp
            harness_name: Name of the harness
            sanitizer: Trial sanitizer for scoped async build selection

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
                dedup_strategy=get_dedup_strategy(self.pov_dedup_strategy),
                inc_image_policy=self.inc_image_policy,
                inc_image_registry=self.inc_image_registry,
                inc_image_max_pull_bytes=self.inc_image_max_pull_bytes,
                inc_image_pull_timeout=self.inc_image_pull_timeout,
                local_image_prefix=self.local_image_prefix,
            )

            # POV output directory (where CRS writes discovered POVs)
            pov_output_dir = trial_output_dir / "output" / "povs"

            # Create POV verification manager
            trial_id = build_trial_uid(
                experiment=self.experiment_name,
                benchmark=benchmark_path.name,
                harness=harness_name,
                trial_dir=trial_output_dir,
            )

            # Use pre-resolved exchange POV path from oss-crs artifacts
            exchange_pov_dir = self.adapter.exchange_pov_dir

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
                exchange_pov_dir=exchange_pov_dir,
                sanitizer=sanitizer,
            )

            self.logger.info(
                f"POV verification manager created: harness={harness_name}, "
                f"expected_cpvs={len(expected_cpv_ids)}, early_stop={self.pov_early_stop}"
            )

            return manager

        except Exception:
            self.logger.exception("Failed to create POV verification manager")
            return None

    def _start_patch_verification_manager(
        self,
        trial_output_dir: Path,
        trial_start_time: float,
        harness_name: str,
        benchmark_id: str,
        target_cpv_id: str | None = None,
    ) -> Optional[PatchVerificationManager]:
        """Start patch verification manager for bug-fixing CRS.

        Only creates manager for bug-fixing CRS.
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
        if self._crs_type != "bug-fixing":
            return None

        try:
            # Patch output directory (where CRS writes patches)
            patch_output_dir = trial_output_dir / "output" / "patches"

            # Get input CPVs count (patches are generated per CPV, not per POV)
            # CPVs are provided as input to bug-fixing CRS
            cpvs_dir = trial_output_dir / "crs-input" / "cpvs"
            input_cpvs_total = len(list(cpvs_dir.iterdir())) if cpvs_dir.exists() else 0

            # Use pre-resolved exchange patch path from oss-crs artifacts
            exchange_patch_dir = self.adapter.exchange_patch_dir

            manager = PatchVerificationManager(
                trial_dir=trial_output_dir,
                patch_output_dir=patch_output_dir,
                harness_name=harness_name,
                benchmark_id=benchmark_id,
                input_cpvs_total=input_cpvs_total,
                trial_start_time=trial_start_time,
                exchange_patch_dir=exchange_patch_dir,
                target_cpv_id=target_cpv_id,
            )

            self.logger.info(
                f"Patch verification manager created: harness={harness_name}, "
                f"input_cpvs={input_cpvs_total}"
            )

            return manager

        except Exception:
            self.logger.exception("Failed to create patch verification manager")
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
        crs_run_end_monotonic: Optional[float] = None,
    ) -> Optional[str]:
        """Stop snapshot and coverage managers."""
        cleanup_errors: list[str] = []
        snapshot_thread_stopped = False
        snapshot_stop_failed = False
        if snapshot_manager:
            self.logger.info("Requesting snapshot manager stop...")
            try:
                snapshot_manager.stop()
            except Exception as e:
                snapshot_stop_failed = True
                msg = f"Snapshot manager cleanup failed: {e}"
                self.logger.warning(msg)
                cleanup_errors.append(msg)

            try:
                if snapshot_thread is None:
                    snapshot_thread_stopped = True
                elif snapshot_thread.is_alive():
                    snapshot_thread.join(timeout=15.0)
                    if snapshot_thread.is_alive():
                        self.logger.warning(
                            "Snapshot thread did not stop within timeout; waiting up to 60s more"
                        )
                        snapshot_thread.join(timeout=60.0)
                        if snapshot_thread.is_alive():
                            msg = (
                                "Snapshot thread did not stop after shutdown wait; "
                                "continuing cleanup with race risk"
                            )
                            self.logger.error(msg)
                            cleanup_errors.append(msg)
                        else:
                            snapshot_thread_stopped = True
                    else:
                        snapshot_thread_stopped = True
                else:
                    snapshot_thread_stopped = True

                if snapshot_thread_stopped:
                    if snapshot_stop_failed:
                        self.logger.warning(
                            "Snapshot stop() failed but thread is stopped; "
                            "continuing with best-effort final snapshot capture"
                        )
            except Exception as e:
                snapshot_thread_stopped = False
                msg = f"Snapshot manager shutdown sequencing failed: {e}"
                self.logger.warning(msg)
                cleanup_errors.append(msg)

        if coverage_manager:
            self.logger.info("Stopping coverage manager...")
            try:
                coverage_manager.stop()
                if coverage_thread and coverage_thread.is_alive():
                    coverage_thread.join(timeout=5.0)
                    if coverage_thread.is_alive():
                        msg = "Coverage thread did not stop within timeout"
                        self.logger.warning(msg)
                        cleanup_errors.append(msg)
            except Exception as e:
                msg = f"Coverage manager cleanup failed: {e}"
                self.logger.warning(msg)
                cleanup_errors.append(msg)

        if snapshot_manager and snapshot_thread_stopped:
            try:
                self._wait_for_llm_accounting_settle(
                    crs_run_end_monotonic=crs_run_end_monotonic
                )
            except Exception as e:
                msg = f"LLM accounting settle wait failed: {e}"
                self.logger.warning(msg)
                cleanup_errors.append(msg)
            self.logger.info(
                f"Capturing final snapshot for harness '{harness_name}'..."
            )
            try:
                snapshot_manager.capture_snapshot()
                snapshot_manager.refresh_final_symlink()
            except Exception as e:
                msg = f"Failed to capture final snapshot: {e}"
                self.logger.warning(msg)
                cleanup_errors.append(msg)

        if cleanup_errors:
            summary = "; ".join(cleanup_errors)
            self.logger.error(
                "Manager cleanup completed with issues for harness '{}': {}",
                harness_name,
                summary,
            )
            return summary
        return None

    def _wait_for_llm_accounting_settle(
        self, *, crs_run_end_monotonic: Optional[float]
    ) -> None:
        """Wait remaining settle time so final LiteLLM accounting can converge."""
        if (
            self.llm_accounting_settle_seconds <= 0
            or crs_run_end_monotonic is None
            or not (self.llm_tracker and self.llm_api_key and self.llm_trial_id)
        ):
            return

        elapsed_since_run_end = max(0.0, time.monotonic() - crs_run_end_monotonic)
        remaining = self.llm_accounting_settle_seconds - elapsed_since_run_end
        if remaining <= 0:
            return

        self.logger.info(
            "Waiting "
            f"{remaining:.1f}s for LiteLLM accounting to settle before final snapshot"
        )
        time.sleep(remaining)

    def _verify_patches(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        oss_fuzz_path: Path,
        harness_name: str,
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
    ) -> list[PatchVerificationResult]:
        """Verify CRS-generated patches for a specific harness.

        Dispatches to distributed verification when Redis is available,
        otherwise falls back to local PatchVerificationEngine.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Path to trial output directory containing patches
            oss_fuzz_path: Path to oss-fuzz directory
            harness_name: Name of the harness

        Returns:
            List of patch verification results
        """
        # Distributed path: enqueue to evaluator queues when Redis is available
        if self.redis_host and self.experiment_name:
            return self._verify_patches_distributed(
                benchmark_path=benchmark_path,
                trial_output_dir=trial_output_dir,
                harness_name=harness_name,
                sanitizer=sanitizer,
                target_cpv_id=target_cpv_id,
            )

        # Local fallback: use PatchVerificationEngine directly
        return self._verify_patches_local(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            oss_fuzz_path=oss_fuzz_path,
            harness_name=harness_name,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
        )

    def _verify_patches_local(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        oss_fuzz_path: Path,
        harness_name: str,
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
    ) -> list[PatchVerificationResult]:
        """Verify patches locally using PatchVerificationEngine.

        This is the original verification path that runs everything on the
        local machine. Used when Redis is not available.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Path to trial output directory containing patches
            oss_fuzz_path: Path to oss-fuzz directory
            harness_name: Name of the harness

        Returns:
            List of patch verification results
        """
        engine: Optional[PatchVerificationEngine] = None
        try:
            self.logger.info(
                f"Starting local patch verification for harness: {harness_name}"
            )

            patch_dir = trial_output_dir / "output" / "patches"
            patches = self._discover_trial_patches_for_verification(
                patch_dir=patch_dir,
                target_cpv_id=target_cpv_id,
            )
            if not patches:
                self.logger.info("No patches found for local verification")
                return []

            # POVs were prepared in trial_output_dir/crs-input/povs/
            pov_dir = trial_output_dir / "crs-input" / "povs"
            if not pov_dir.exists():
                self.logger.warning(f"No POVs directory found: {pov_dir}")
                return []

            # Patch verification logs are written under trial/patches/logs/
            patch_artifacts_dir = trial_output_dir / "patches"
            patch_artifacts_dir.mkdir(parents=True, exist_ok=True)

            engine = PatchVerificationEngine(
                oss_fuzz_path=oss_fuzz_path,
                sanitizer=sanitizer,
                timeout=self.per_pov_verify_timeout,
                build_timeout=1200,
                test_timeout=1800,
                log_dir=patch_artifacts_dir,
                force_rebuild=True,  # Always rebuild for fresh verification
                verify_variants=self.patch_verify_variants,
                inc_image_policy=self.inc_image_policy,
                inc_image_registry=self.inc_image_registry,
                inc_image_max_pull_bytes=self.inc_image_max_pull_bytes,
                inc_image_pull_timeout=self.inc_image_pull_timeout,
                local_image_prefix=self.local_image_prefix,
            )

            results: list[PatchVerificationResult] = []
            for cpv_id, patch_id, patch_path in patches:
                pov_path = self._find_trial_pov_for_cpv(pov_dir, cpv_id)
                if pov_path is None:
                    results.append(
                        PatchVerificationResult(
                            status=PatchVerificationStatus.ERROR,
                            patch_id=patch_id,
                            pov_id=cpv_id,
                            benchmark=str(benchmark_path.name),
                            patch_path=patch_path,
                            details=f"POV not found for {cpv_id}",
                        )
                    )
                    continue

                patch = PatchInfo(
                    patch_id=patch_id,
                    pov_id=cpv_id,
                    patch_path=patch_path,
                )
                results.append(
                    engine.verify_patch(
                        benchmark_path=benchmark_path,
                        patch=patch,
                        harness=harness_name,
                        pov_path=pov_path,
                    )
                )

            # Log summary
            valid_count = sum(1 for r in results if r.is_valid)
            self.logger.info(
                f"Patch verification completed: {valid_count}/{len(results)} patches valid"
            )

            return results

        except PatchDiscoveryError:
            raise
        except Exception:
            self.logger.exception(
                "Patch verification failed for harness '{}'",
                harness_name,
            )
            return []
        finally:
            if engine is not None:
                engine.cleanup()

    def _verify_patches_distributed(
        self,
        benchmark_path: Path,
        trial_output_dir: Path,
        harness_name: str,
        sanitizer: str = "address",
        target_cpv_id: str | None = None,
    ) -> list[PatchVerificationResult]:
        """Verify patches via distributed evaluator queues.

        Enqueues patch build jobs to the BUILD queue (multi-CPU) and verify
        jobs to the VERIFY queue (1 CPU) with RQ dependency. Drains results
        and converts them to PatchVerificationResult format.

        Falls back to local verification if Redis is unreachable or enqueue
        fails.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Path to trial output directory containing patches
            harness_name: Name of the harness

        Returns:
            List of patch verification results
        """
        from crsbench.distributed.patch_queue import (
            drain_patch_verdicts,
            enqueue_patch_jobs,
            initialize_patch_queues,
        )

        # Narrow Optional types -- caller already checked these are non-None
        redis_host: str = self.redis_host  # type: ignore[assignment]
        experiment_name: str = self.experiment_name  # type: ignore[assignment]

        try:
            self.logger.info(
                f"Starting distributed patch verification for harness: {harness_name}"
            )

            patch_dir = trial_output_dir / "output" / "patches"
            patches = self._discover_trial_patches_for_verification(
                patch_dir=patch_dir,
                target_cpv_id=target_cpv_id,
            )

            if not patches:
                self.logger.info("No patches found for distributed verification")
                return []

            # Initialize queues
            build_queue, verify_queue = initialize_patch_queues(
                redis_host, experiment_name
            )
            if build_queue is None or verify_queue is None:
                self.logger.warning(
                    "Redis unavailable for patch verification, falling back to local"
                )
                return self._verify_patches_local(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    oss_fuzz_path=self.oss_fuzz_path or Path(),
                    harness_name=harness_name,
                    sanitizer=sanitizer,
                    target_cpv_id=target_cpv_id,
                )

            trial_id = build_trial_uid(
                experiment=self.experiment_name,
                benchmark=benchmark_path.name,
                harness=harness_name,
                trial_dir=trial_output_dir,
            )

            # Enqueue all patch jobs
            job_ids = enqueue_patch_jobs(
                build_queue,
                verify_queue,
                experiment_name,
                trial_id,
                benchmark_path.name,
                harness_name,
                patches,
                sanitizer=sanitizer,
                verify_variants=self.patch_verify_variants,
                use_inc_build=True,
            )

            if not job_ids:
                self.logger.warning("All patch enqueues failed, falling back to local")
                return self._verify_patches_local(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    oss_fuzz_path=self.oss_fuzz_path or Path(),
                    harness_name=harness_name,
                    sanitizer=sanitizer,
                    target_cpv_id=target_cpv_id,
                )

            # Drain results (blocking poll)
            raw_results = drain_patch_verdicts(
                redis_host,
                job_ids,
                timeout=self.verify_timeout,
            )

            # Convert results to PatchVerificationResult
            results: list[PatchVerificationResult] = []
            for result in raw_results:
                try:
                    # Map status string to PatchVerificationStatus enum
                    status_str = result.get("status", "error")
                    try:
                        status = PatchVerificationStatus(status_str)
                    except ValueError:
                        status = PatchVerificationStatus.ERROR

                    patch_path_str = result.get("patch_path", "")
                    patch_path = Path(patch_path_str) if patch_path_str else Path()

                    pvr = PatchVerificationResult(
                        status=status,
                        patch_id=result.get("patch_id", ""),
                        pov_id=result.get("cpv_id", ""),
                        benchmark=result.get("benchmark", ""),
                        patch_path=patch_path,
                        harness=result.get("harness", ""),
                        details=result.get("details", ""),
                        pov_test_passed=result.get("pov_test_passed"),
                        unit_tests_passed=result.get("unit_test_passed"),
                        build_time=result.get("build_time", 0.0),
                        pov_test_time=result.get("pov_test_time", 0.0),
                        unit_test_time=result.get("unit_test_time", 0.0),
                        elapsed_seconds=result.get("elapsed_seconds", 0.0),
                        cpv_fixed=result.get("cpv_fixed", []),
                        security_verdict=result.get("security_verdict", "FAIL"),
                        failed_tests=result.get("failed_tests", []),
                    )

                    # Handle optional complex types
                    cpv_stats_raw = result.get("cpv_stats")
                    if cpv_stats_raw and isinstance(cpv_stats_raw, dict):
                        from crsbench.evaluation.verification.models import CpvStats

                        for cpv_id, stats_data in cpv_stats_raw.items():
                            if isinstance(stats_data, dict):
                                pvr.cpv_stats[cpv_id] = CpvStats(
                                    cpv_id=stats_data.get("cpv_id", cpv_id),
                                    variants_tested=stats_data.get(
                                        "variants_tested", 0
                                    ),
                                    variants_matched=stats_data.get(
                                        "variants_matched", 0
                                    ),
                                    variant_results=stats_data.get(
                                        "variant_results", {}
                                    ),
                                )

                    scores_raw = result.get("scores")
                    if scores_raw and isinstance(scores_raw, dict):
                        from crsbench.evaluation.verification.models import (
                            VerificationScores,
                        )

                        pvr.scores = VerificationScores(
                            cpvs_complete=scores_raw.get("cpvs_complete", 0),
                            cpvs_partial=scores_raw.get("cpvs_partial", 0),
                            cpvs_none=scores_raw.get("cpvs_none", 0),
                            total_variants_tested=scores_raw.get(
                                "total_variants_tested", 0
                            ),
                            total_variants_matched=scores_raw.get(
                                "total_variants_matched", 0
                            ),
                        )

                    results.append(pvr)

                except Exception as e:
                    self.logger.warning(
                        f"Failed to convert distributed patch result: {e}"
                    )

            valid_count = sum(1 for r in results if r.is_valid)
            self.logger.info(
                f"Distributed patch verification completed: "
                f"{valid_count}/{len(results)} patches valid"
            )

            return results

        except PatchDiscoveryError:
            raise
        except Exception:
            self.logger.exception(
                "Distributed patch verification failed for harness '{}'",
                harness_name,
            )
            return []

    def _discover_trial_patches_for_verification(
        self,
        patch_dir: Path,
        target_cpv_id: str | None,
    ) -> list[tuple[str, str, Path]]:
        """Discover trial patches for verification.

        Supports two layouts:
        ``output/patches/<cpv_id>/*.diff`` (structured) and
        ``output/patches/*.diff`` (flat).
        """
        if not patch_dir.exists():
            self.logger.info(
                f"No patches directory found (CRS produced no patches): {patch_dir}"
            )
            return []

        structured_patches: list[tuple[str, str, Path]] = []
        structured_dirs = sorted(
            entry for entry in patch_dir.iterdir() if entry.is_dir()
        )
        for cpv_dir in structured_dirs:
            cpv_id = cpv_dir.name
            cpv_patches = sorted(
                entry
                for entry in cpv_dir.iterdir()
                if entry.is_file() and entry.suffix == ".diff"
            )
            for patch_path in cpv_patches:
                patch_id = self._build_trial_patch_id(
                    layout="structured",
                    cpv_id=cpv_id,
                    patch_path=patch_path,
                    patch_dir=patch_dir,
                )
                structured_patches.append((cpv_id, patch_id, patch_path))

        flat_patches = sorted(
            entry
            for entry in patch_dir.iterdir()
            if entry.is_file() and entry.suffix == ".diff"
        )
        flat_discovered: list[tuple[str, str, Path]] = []
        if flat_patches:
            if self._crs_type == "bug-fixing" and not target_cpv_id:
                raise PatchDiscoveryError(
                    "Flat patch output requires target_cpv_id for bug-fixing trials."
                )
            cpv_id = target_cpv_id or "unknown"
            for patch_path in flat_patches:
                patch_id = self._build_trial_patch_id(
                    layout="flat",
                    cpv_id=cpv_id,
                    patch_path=patch_path,
                    patch_dir=patch_dir,
                )
                flat_discovered.append((cpv_id, patch_id, patch_path))

        discovered = structured_patches + flat_discovered
        deduped: list[tuple[str, str, Path]] = []
        seen_keys: set[tuple[str, str]] = set()
        for cpv_id, patch_id, patch_path in discovered:
            key = (cpv_id, patch_id)
            if key in seen_keys:
                self.logger.warning(
                    "Skipping duplicate discovered patch identity: "
                    f"cpv_id={cpv_id}, patch_id={patch_id}, path={patch_path}"
                )
                continue
            seen_keys.add(key)
            deduped.append((cpv_id, patch_id, patch_path))
        if not deduped:
            return []
        self.logger.info(
            "Discovered patches for verification: "
            f"structured={len(structured_patches)}, flat={len(flat_discovered)}, "
            f"total={len(deduped)}, "
            f"target_cpv_id={target_cpv_id or '-'}"
        )
        return deduped

    @staticmethod
    def _build_trial_patch_id(
        *,
        layout: str,
        cpv_id: str,
        patch_path: Path,
        patch_dir: Path,
    ) -> str:
        """Build a stable, collision-resistant patch ID within a trial."""
        rel_path = patch_path.relative_to(patch_dir).as_posix()
        name_part = re.sub(r"[^a-zA-Z0-9_-]+", "_", patch_path.stem).strip("_")
        if not name_part:
            name_part = "patch"
        digest = hashlib.sha1(f"{layout}:{cpv_id}:{rel_path}".encode()).hexdigest()[:10]
        return f"{layout}_{name_part}_{digest}"

    @staticmethod
    def _find_trial_pov_for_cpv(pov_dir: Path, cpv_id: str) -> Optional[Path]:
        """Find staged trial POV path for the given CPV."""
        direct = pov_dir / cpv_id
        if direct.exists() and direct.is_file():
            return direct

        for ext in [".blob", ".pov", ".bin"]:
            candidate = pov_dir / f"{cpv_id}{ext}"
            if candidate.exists() and candidate.is_file():
                return candidate

        cpv_subdir = pov_dir / cpv_id
        if cpv_subdir.is_dir():
            for child in sorted(cpv_subdir.iterdir()):
                if child.is_file():
                    return child

        return None

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
            corpus_dir = trial_output_dir / "output" / "seeds"

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

        except Exception:
            self.logger.exception("Failed to create coverage manager")
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
        corpus_dir = trial_output_dir / "output" / "seeds"
        if not corpus_dir.exists() or not any(corpus_dir.iterdir()):
            self.logger.info("No corpus files for post-experiment coverage")
            return

        try:
            self.logger.info(
                f"Running post-experiment coverage on {corpus_dir} "
                f"({len(list(corpus_dir.iterdir()))} files)"
            )
            from crsbench.evaluation.coverage.engine import CoverageEngine
            from crsbench.evaluation.coverage.timeline import normalize_seed_inputs

            output_dir = trial_output_dir / "coverage"
            normalized_inputs = normalize_seed_inputs(corpus_dir, base_time=None)
            if not normalized_inputs:
                self.logger.info("No analyzable seeds for post-experiment coverage")
                return

            engine = CoverageEngine()
            try:
                _, summary = engine.collect_timed_line_coverage(
                    benchmark_path=benchmark_path,
                    timed_inputs=normalized_inputs,
                    harness_filter=harness_name,
                    output_dir=output_dir,
                )
            finally:
                engine.cleanup()

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

            self.logger.info(f"Post-experiment coverage: {summary.format_lines()}")

        except Exception:
            self.logger.exception("Post-experiment coverage failed")

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
