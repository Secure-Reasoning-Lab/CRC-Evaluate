"""Main benchmark runner for CRS evaluation."""

import os
import re
import logging
from pathlib import Path
from typing import Union, Optional, Dict, Any, List
from crsbench.validation import validate_benchmark, ValidationResult
from crsbench.validation.schemas import BenchmarkConfig, HarnessFile, POV
from crsbench.evaluation.crs_executor import CRSExecutor, StubCRSExecutor
from crsbench.evaluation.results import ResultCollector, EvaluationReport, HarnessResult, POVResult, POVStatus

# Set up logging
logger = logging.getLogger(__name__)


class EvaluationError(Exception):
    """Exception raised during benchmark evaluation."""
    pass


class EvaluationResult:
    """Result from a benchmark evaluation."""

    def __init__(self, report: EvaluationReport, validation_result: ValidationResult):
        self.report = report
        self.validation_result = validation_result

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

    def __init__(self, crs_executor: Optional[CRSExecutor] = None):
        """Initialize benchmark runner.

        Args:
            crs_executor: CRS executor instance. If None, uses stub executor.
        """
        self.crs_executor = crs_executor or StubCRSExecutor()
        self.logger = logging.getLogger(__name__)

    def run_benchmark(self, benchmark_path: Union[str, Path],
                      mode: Optional[str] = None,
                      crs_config: Optional[Dict[str, Any]] = None) -> EvaluationResult:
        """Run a complete benchmark evaluation.

        Args:
            benchmark_path: Path to benchmark directory or meta.yaml
            mode: Evaluation mode ('delta', 'full', or 'auto' to detect)
            crs_config: Configuration for CRS executor

        Returns:
            EvaluationResult: Complete evaluation results

        Raises:
            EvaluationError: If evaluation fails
        """
        benchmark_path = Path(benchmark_path)

        self.logger.info(f"Starting benchmark evaluation: {benchmark_path}")

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
            config = self._load_benchmark_config(benchmark_path, validation_result)

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
                collector.set_commits(config.delta_mode.base_commit, config.delta_mode.ref_commit)
            elif evaluation_mode == "full" and config.full_mode:
                collector.set_commits(config.full_mode.base_commit)

            if crs_config:
                collector.set_crs_config(crs_config)

            # Step 7: Run evaluation on each harness
            self._run_harness_evaluations(config, benchmark_path, collector, evaluation_mode)

            # Step 8: Generate final report
            report = collector.finalize_report()

            self.logger.info(f"Evaluation completed: {report.povs_found}/{report.total_povs} POVs detected "
                           f"({report.success_rate:.1%} success rate)")

            return EvaluationResult(report, validation_result)

        except Exception as e:
            self.logger.error(f"Benchmark evaluation failed: {str(e)}")
            raise EvaluationError(f"Failed to evaluate benchmark: {str(e)}") from e

    def _load_benchmark_config(self, benchmark_path: Path,
                              validation_result: ValidationResult) -> BenchmarkConfig:
        """Load benchmark configuration from validation result or file."""
        # For now, we need to re-parse since validation doesn't return the config
        # This is a limitation we could improve in the validation module later
        import yaml

        meta_yaml_path = self._resolve_meta_yaml_path(benchmark_path)

        if not meta_yaml_path.exists():
            raise EvaluationError(f"meta.yaml not found at {meta_yaml_path}")

        with open(meta_yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)


        try:
            return BenchmarkConfig(**data)
        except Exception as e:
            # If validation failed, create minimal config to continue
            self.logger.warning(f"Failed to parse config, using minimal fallback: {e}")
            from crsbench.validation.schemas import HarnessFile, FullMode
            dummy_harness = HarnessFile(name="dummy", path="$REPO/dummy.c")
            dummy_full_mode = FullMode(base_commit="abc123def456")
            return BenchmarkConfig(harness_files=[dummy_harness], full_mode=dummy_full_mode)

    def _resolve_meta_yaml_path(self, path: Path) -> Path:
        """Resolve the path to meta.yaml file."""
        if path.is_file() and path.name == "meta.yaml":
            return path
        elif path.is_dir():
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
        else:
            # Assume it's meant to be meta.yaml file
            return path

    def _determine_evaluation_mode(self, config: BenchmarkConfig, mode: Optional[str]) -> str:
        """Determine the evaluation mode to use."""
        if mode and mode in ["delta", "full"]:
            # Validate that the requested mode is available
            if mode == "delta" and not config.delta_mode:
                raise EvaluationError("Delta mode requested but not configured in benchmark")
            if mode == "full" and not config.full_mode:
                raise EvaluationError("Full mode requested but not configured in benchmark")
            return mode
        elif mode == "auto" or mode is None:
            # Auto-detect based on available configuration
            if config.delta_mode:
                return "delta"
            elif config.full_mode:
                return "full"
            else:
                raise EvaluationError("No evaluation mode available in benchmark configuration")
        else:
            raise EvaluationError(f"Invalid evaluation mode: {mode}")

    def _run_harness_evaluations(self, config: BenchmarkConfig, benchmark_path: Path,
                                collector: ResultCollector, evaluation_mode: str) -> None:
        """Run CRS evaluation on all harnesses."""
        # Get commit information based on mode
        if evaluation_mode == "delta" and config.delta_mode:
            base_commit = config.delta_mode.base_commit
            ref_commit = config.delta_mode.ref_commit
        elif evaluation_mode == "full" and config.full_mode:
            base_commit = config.full_mode.base_commit
            ref_commit = None
        else:
            raise EvaluationError(f"Invalid configuration for mode {evaluation_mode}")

        self.logger.info(f"Running evaluation on {len(config.harness_files)} harnesses...")

        for harness in config.harness_files:
            self.logger.info(f"Evaluating harness: {harness.name}")

            try:
                # Run CRS on this harness (path resolution handled internally in CRS)
                crs_result = self.crs_executor.run_crs(
                    benchmark_path=benchmark_path,
                    harness=harness,
                    base_commit=base_commit,
                    ref_commit=ref_commit
                )

                # Process POV results
                pov_results = []
                if harness.povs:
                    pov_results = self.crs_executor.process_pov_results(crs_result, harness)
                else:
                    self.logger.warning(f"Harness '{harness.name}' has no POVs configured")

                # Create harness result
                harness_result = HarnessResult(
                    name=harness.name,
                    path=harness.path,
                    pov_results=pov_results,
                    execution_time=crs_result.execution_time,
                    build_successful=crs_result.success,
                    build_output=crs_result.output
                )

                collector.add_harness_result(harness_result)

                # Log results for this harness
                if pov_results:
                    found_count = sum(1 for pov in pov_results if pov.status == POVStatus.FOUND)
                    total_count = len(pov_results)
                    self.logger.info(f"  {found_count}/{total_count} POVs detected in {harness.name}")

            except Exception as e:
                self.logger.error(f"Failed to evaluate harness '{harness.name}': {str(e)}")
                # Create error result for this harness
                pov_results = []
                if harness.povs:
                    for pov in harness.povs:
                        pov_results.append(POVResult(
                            name=pov.name,
                            harness_name=harness.name,
                            sanitizer=pov.sanitizer,
                            error_token=pov.error_token,
                            status=POVStatus.ERROR,
                            error_message=str(e)
                        ))

                harness_result = HarnessResult(
                    name=harness.name,
                    path=harness.path,
                    pov_results=pov_results,
                    execution_time=0.0,
                    build_successful=False,
                    build_output=f"Error: {str(e)}"
                )

                collector.add_harness_result(harness_result)

    def _resolve_path_variables(self, path: str, benchmark_path: Path) -> str:
        """Resolve path variables like $REPO and $PROJECT.

        $REPO refers to the benchmark repository root directory.
        $PROJECT refers to the project directory (distinct from $REPO).
        """
        resolved = path

        # Replace $REPO with benchmark path
        if "$REPO/" in resolved:
            resolved = resolved.replace("$REPO/", str(benchmark_path) + "/")

        # $PROJECT is handled differently - it may refer to a project subdirectory
        # For now, we keep it as-is since the actual CRS implementation will handle it
        # This is a placeholder for when we know the actual $PROJECT resolution logic

        return resolved