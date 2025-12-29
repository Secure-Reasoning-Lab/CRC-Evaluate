"""Main benchmark runner for CRS evaluation."""

import threading
import time
from pathlib import Path
from typing import Any, Optional

from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_executor import CRSExecutor, StubCRSExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.results import EvaluationReport, HarnessResult, ResultCollector
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.utils.logger import get_logger
from crsbench.validation import ValidationResult, VerificationEngine, validate_benchmark
from crsbench.validation import VerificationResult as VerifResult
from crsbench.validation.schemas import BenchmarkConfig, BenchmarkHarness, HarnessFile
from crsbench.validation.verification.dedup import PatchBasedDedup

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
        verification_results: Optional[list] = None,
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
        *,
        coverage_enabled: bool = False,
        coverage_saturation_time: int = 21600,
        coverage_early_stop: bool = False,
        oss_fuzz_path: Optional[Path] = None,
    ):
        """Initialize benchmark runner.

        Args:
            crs_executor: CRS executor instance. If None, uses stub executor.
            snapshot_period: Snapshot interval in seconds (0 or None to disable)
            coverage_enabled: Enable coverage collection during trials
            coverage_saturation_time: Seconds without new coverage to detect saturation
            coverage_early_stop: Terminate trial early on saturation (NOT YET IMPLEMENTED)
            oss_fuzz_path: Path to oss-fuzz directory (required for coverage)
        """
        self.crs_executor = crs_executor or StubCRSExecutor()
        self.snapshot_period = snapshot_period
        self.coverage_enabled = coverage_enabled
        self.coverage_saturation_time = coverage_saturation_time
        self.coverage_early_stop = coverage_early_stop
        self.oss_fuzz_path = oss_fuzz_path
        self.logger = get_logger(__name__)

        if coverage_early_stop:
            self.logger.warning(
                "coverage_early_stop=True but early termination is NOT YET IMPLEMENTED. "
                "Trial will run until max_total_time."
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
    ) -> tuple[HarnessResult, list[VerifResult]]:
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
        coverage_manager = None
        coverage_thread = None
        verification_results: list[VerifResult] = []
        harness_result = None

        self.logger.info(f"Evaluating harness: {harness.name}")
        self.logger.debug(
            f"Coverage settings: enabled={self.coverage_enabled}, "
            f"oss_fuzz_path={self.oss_fuzz_path}"
        )

        try:
            # Set up coverage manager if enabled
            if self.coverage_enabled and self.oss_fuzz_path:
                coverage_manager = self._create_coverage_manager(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    trial_start_time=trial_start_time,
                    harness_name=harness.name,
                )
                if coverage_manager:
                    coverage_thread = threading.Thread(
                        target=coverage_manager.run, daemon=True
                    )
                    coverage_thread.start()
                    self.logger.info("Coverage manager thread started")

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
                    coverage_manager=coverage_manager,
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
                run_successful=crs_result.success,
                run_output=crs_result.output,
            )

        except Exception as e:
            self.logger.error(f"Failed to evaluate harness '{harness.name}': {str(e)}")
            # Create error result
            harness_result = HarnessResult(
                name=harness.name,
                path=harness.path,
                execution_time=0.0,
                run_successful=False,
                run_output=f"Error: {str(e)}",
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

            # Stop coverage manager thread
            if coverage_manager:
                self.logger.info("Stopping coverage manager...")
                coverage_manager.stop()
                if coverage_thread and coverage_thread.is_alive():
                    coverage_thread.join(timeout=5.0)
                    if coverage_thread.is_alive():
                        self.logger.warning(
                            "Coverage thread did not stop within timeout"
                        )

            # Run post-experiment coverage on final corpus
            if self.coverage_enabled and self.oss_fuzz_path and harness_result:
                self._run_post_experiment_coverage(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    harness_name=harness.name,
                )

        # Verify POVs AFTER snapshot thread has stopped and final snapshot captured
        if (
            harness_result
            and harness_result.run_successful
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
        else:
            # Log why verification was skipped
            if not harness_result or not harness_result.run_successful:
                self.logger.info("POV verification skipped: run unsuccessful")
            elif skip_verification:
                self.logger.info("POV verification skipped: verification disabled")
            elif not isinstance(self.crs_executor, CRSBugFindingExecutor):
                self.logger.info("POV verification skipped: not a bug finding executor")
            elif not oss_fuzz_path:
                self.logger.info(
                    "POV verification skipped: oss-fuzz path not available"
                )

        return harness_result, verification_results

    def _verify_povs(
        self,
        benchmark_path: Path,
        crs_output_dir: Path,
        oss_fuzz_path: Path,
        harness_name: str,
    ) -> list[VerifResult]:
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
                dedup_strategy=PatchBasedDedup(),  # TODO: make it configurable
            )
            pov_dir = crs_output_dir / "povs"
            return engine.verify_benchmark(
                benchmark_path=benchmark_path,
                pov_dir=pov_dir,
                deduplicate=True,  # TODO: configurable?
                harness_filter=harness_name,
                force_rebuild=True,  # Always rebuild to ensure correct patches
            )
        except Exception as e:
            self.logger.error(
                f"POV verification failed for harness '{harness_name}': {e}",
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

            from crsbench.evaluation.coverage import CoverageManager
            from crsbench.evaluation.coverage.builder import CoverageBuilder
            from crsbench.evaluation.coverage.collector import CoverageCollector
            from crsbench.evaluation.coverage.models import CoverageConfig
            from crsbench.evaluation.coverage.store import CoverageStore
            from crsbench.evaluation.coverage.strategy import create_coverage_strategy

            # Determine language from benchmark config
            # Look for language in project.yaml
            project_yaml = benchmark_path / "project.yaml"
            language = "c"  # default
            if project_yaml.exists():
                with project_yaml.open() as f:
                    project_config = yaml.safe_load(f)
                    language = project_config.get("language", "c")

            # Get project name for coverage variant
            project_name = benchmark_path.name
            coverage_variant = f"{project_name}-coverage"

            # Build coverage variant if not already built
            builder = CoverageBuilder(self.oss_fuzz_path)
            if not builder.is_built(project_name):
                self.logger.info(f"Building coverage variant: {coverage_variant}")

                # Read project.yaml for main_repo (not meta.yaml!)
                project_yaml = benchmark_path / "project.yaml"
                if not project_yaml.exists():
                    self.logger.error(f"project.yaml not found: {project_yaml}")
                    return None

                with project_yaml.open() as f:
                    project_config = yaml.safe_load(f)

                main_repo = project_config.get("main_repo")
                repo_name = project_config.get("repo_name")

                # Read meta.yaml for commit info
                meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
                if not meta_yaml.exists():
                    self.logger.error(f"meta.yaml not found: {meta_yaml}")
                    return None

                with meta_yaml.open() as f:
                    meta = yaml.safe_load(f)

                # Get target commit (ref_commit for delta mode)
                delta_mode = meta.get("delta_mode", {})
                target_commit = delta_mode.get("ref_commit")
                if not target_commit:
                    full_mode = meta.get("full_mode", {})
                    target_commit = full_mode.get("base_commit")

                if not main_repo or not target_commit:
                    self.logger.error(
                        "main_repo not found in project.yaml or target_commit not found in meta.yaml"
                    )
                    return None

                build_result = builder.build(
                    project_name=project_name,
                    benchmark_path=benchmark_path,
                    main_repo=main_repo,
                    commit=target_commit,
                    language=language,
                    repo_name=repo_name,
                )
                if not build_result:
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
            collector = CoverageCollector(
                strategy, store, harness_name, output_dir=coverage_store_dir
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
            from crsbench.evaluation.coverage.models import CoverageSummary
            from crsbench.evaluation.coverage.strategy import (
                create_coverage_strategy,
                parse_llvm_cov_summary,
            )

            # Determine language
            project_yaml = benchmark_path / "project.yaml"
            language = "c"
            if project_yaml.exists():
                import yaml

                with project_yaml.open() as f:
                    project_config = yaml.safe_load(f)
                    language = project_config.get("language", "c")

            # Get coverage variant name
            project_name = benchmark_path.name
            coverage_variant = f"{project_name}-coverage"

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
