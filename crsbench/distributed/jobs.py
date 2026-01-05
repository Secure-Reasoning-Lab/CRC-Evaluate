"""Job definitions for CRSBench distributed execution.

This module defines all job types that can be executed by workers in the distributed
job queue system. Jobs are enqueued by the orchestrator and executed by workers.
"""

import time
from pathlib import Path
from typing import Any, Dict

import yaml

from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.results import CRSType, TrialMetadata, TrialResult
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.utils.crs_helper import get_crs_registry_name
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_crs_type(crs_name: str, registry_dir: Path) -> str:
    """Read CRS type from pkg.yaml in registry.

    Args:
        crs_name: Name of the CRS
        registry_dir: Path to CRS registry directory

    Returns:
        CRS type: 'bug-finding' or 'bug-fixing'

    Raises:
        FileNotFoundError: If pkg.yaml not found
        ValueError: If type field is missing or invalid
    """
    pkg_yaml_path = registry_dir / crs_name / "pkg.yaml"

    if not pkg_yaml_path.exists():
        raise FileNotFoundError(f"CRS package file not found: {pkg_yaml_path}")

    with pkg_yaml_path.open("r") as f:
        pkg_data = yaml.safe_load(f)

    crs_type = pkg_data.get("type")
    if not crs_type:
        raise ValueError(f"CRS type not specified in {pkg_yaml_path}")

    if crs_type not in ["bug-finding", "bug-fixing"]:
        raise ValueError(
            f"Invalid CRS type '{crs_type}' in {pkg_yaml_path}. Must be 'bug-finding' or 'bug-fixing'"
        )

    return crs_type


def build_crs_environment(
    crs: str, benchmark: str, _config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Prepare CRS execution environment.

    This job handles environment setup tasks before CRS trial execution:
    - Validate CRS installation
    - Prepare Docker images if needed
    - Set up CRS-specific configuration
    - Verify benchmark accessibility

    Args:
        crs: CRS implementation name (e.g., 'atlantis-c', 'test-crs')
        benchmark: Benchmark identifier or path
        config: Experiment configuration dictionary

    Returns:
        dict: Environment setup result with status and metadata

    Example:
        >>> result = build_crs_environment('test-crs', 'test-benchmark', {})
        >>> assert result['success'] is True
    """
    logger.info(f"Building environment for CRS '{crs}' on benchmark '{benchmark}'")

    try:
        # TODO: Implement actual CRS environment setup
        # For now, this is a placeholder that always succeeds

        # Validate CRS exists
        # Prepare Docker images
        # Set up CRS configuration
        # Verify benchmark path

        result = {
            "success": True,
            "crs": crs,
            "benchmark": benchmark,
            "environment_ready": True,
            "metadata": {"setup_time": 0.0, "timestamp": time.time()},
        }

        logger.info(f"Environment setup completed for {crs}")
        return result

    except Exception as e:
        logger.error(f"Environment setup failed for {crs}: {e}")
        return {
            "success": False,
            "crs": crs,
            "benchmark": benchmark,
            "environment_ready": False,
            "error": str(e),
            "metadata": {"timestamp": time.time()},
        }


def run_crs_trial(
    crs: str,
    benchmark: str,
    harness_name: str,
    harness_path: str,
    trial_num: int,
    config: Dict[str, Any],
    mode: str,
) -> TrialResult:
    """
    Execute a single CRS trial.

    This is the main job type for CRS evaluation. It runs a complete trial
    for one CRS on one benchmark harness, including:
    - Loading benchmark configuration
    - Executing CRS
    - Collecting results
    - Storing trial data

    Args:
        crs: CRS implementation name
        benchmark: Benchmark identifier or path
        harness_name: Harness name to run
        harness_path: Path to harness file
        trial_num: Trial number (1-indexed) for this execution
        config: Experiment configuration dictionary
        mode: Evaluation mode ('delta', 'full', or 'all')

    Returns:
        TrialResult: Trial results including POVs found, success rate, and metadata

    Example:
        >>> config = {
        ...     'experiment_filestore': '/tmp/exp',
        ...     'max_total_time': 3600
        ... }
        >>> result = run_crs_trial(
        ...     'test-crs', 'test-benchmark', 'fuzz_test', '/src/fuzz_test.c', 1, config, 'delta'
        ... )
        >>> assert result.povs_found >= 0
    """
    logger.info(
        f"[Trial {trial_num}] Starting CRS '{crs}' on benchmark '{benchmark}' harness '{harness_name}'"
    )
    start_time = time.time()
    crs_type_enum = CRSType.BUG_FINDING  # Default, updated after detection

    try:
        # Get snapshot configuration
        snapshot_period = config.get("snapshot_period")

        # Initialize CRS executor
        # Get required paths from config or use defaults
        # Resolve to absolute paths to avoid issues with relative paths
        oss_fuzz_path = Path(
            config.get("oss_fuzz_path") or (Path.cwd() / "oss-fuzz")
        ).resolve()
        registry_dir = Path(
            config.get("registry_dir") or (Path.cwd() / "crses" / "registry")
        ).resolve()
        benchmarks_root = Path(
            config.get("benchmarks_root") or (Path.cwd() / "benchmarks")
        ).resolve()
        crs_configs_dir = Path(
            config.get("crs_configs_dir") or (Path.cwd() / "crses" / "configs")
        ).resolve()

        # Resolve CRS config name to registry name
        registry_name = get_crs_registry_name(crs, crs_configs_dir)
        logger.info(f"Resolved CRS config '{crs}' to registry '{registry_name}'")

        # Detect CRS type from registry
        crs_type = get_crs_type(registry_name, registry_dir)
        crs_type_enum = (
            CRSType.BUG_FIXING if crs_type == "bug-fixing" else CRSType.BUG_FINDING
        )
        logger.info(f"Detected CRS type '{crs_type}' for CRS '{crs}'")

        # Create appropriate executor based on CRS type
        if crs_type == "bug-fixing":
            # Patch generation CRS
            crs_executor = CRSPatchExecutor(
                crs_config_name=crs,
                oss_fuzz_path=oss_fuzz_path,
                registry_dir=registry_dir,
                benchmarks_root=benchmarks_root,
                crs_configs_dir=crs_configs_dir,
                litellm_mode=config.get("litellm_mode", "passthrough"),
            )
        else:
            # Bug finding CRS
            crs_executor = CRSBugFindingExecutor(
                crs_config_name=crs,
                oss_fuzz_path=oss_fuzz_path,
                registry_dir=registry_dir,
                benchmarks_root=benchmarks_root,
                crs_configs_dir=crs_configs_dir,
                litellm_mode=config.get("litellm_mode", "passthrough"),
            )

        # Configure executor
        crs_executor.configure_crs(
            {
                "build_timeout": config.get("build_timeout", 3600),
                "run_timeout": config.get("max_total_time", 7200),
                "hints_enabled": config.get("hints_enabled", False),
                "hint_sarif_level": config.get("hint_sarif_level"),
                "hint_corpus_level": config.get("hint_corpus_level"),
                "project_image_prefix": config.get("project_image_prefix", "aixcc-afc"),
                "mode": mode,
            }
        )

        # Initialize benchmark runner with CRS executor and snapshot configuration
        coverage_enabled = config.get("coverage_enabled", False)
        coverage_saturation_time = config.get("coverage_saturation_time", 21600)
        coverage_early_stop = config.get("coverage_early_stop", False)
        # Handle None from config (when key exists but value is None)
        oss_fuzz_path_str = config.get("oss_fuzz_path") or "oss-fuzz"
        oss_fuzz_path = Path(oss_fuzz_path_str)
        logger.debug(
            f"Coverage config: enabled={coverage_enabled}, "
            f"saturation_time={coverage_saturation_time}, "
            f"early_stop={coverage_early_stop}, oss_fuzz_path={oss_fuzz_path}"
        )

        runner = BenchmarkRunner(
            crs_executor,
            snapshot_period=snapshot_period,
            coverage_enabled=coverage_enabled,
            coverage_saturation_time=coverage_saturation_time,
            coverage_early_stop=coverage_early_stop,
            oss_fuzz_path=oss_fuzz_path if coverage_enabled else None,
        )

        # Resolve benchmark path
        benchmark_path = _resolve_benchmark_path(benchmark, config)
        logger.debug(f"Resolved benchmark path: {benchmark_path}")

        # Ensure original project symlink exists in oss-fuzz/projects/
        # This allows tools like oss-bugfix-crs to find the project
        _ensure_project_symlink(oss_fuzz_path, benchmark, benchmark_path)

        # Create BenchmarkHarness object
        from crsbench.validation.schemas import BenchmarkHarness, HarnessFile

        harness = HarnessFile(name=harness_name, path=harness_path)
        benchmark_harness = BenchmarkHarness(
            name=benchmark, path=benchmark_path, harness=harness
        )

        # Create trial output directory with harness-specific structure
        experiment_filestore = Path(
            config.get("experiment_filestore", "/tmp/experiments")
        ).resolve()
        experiment_name = config.get("experiment", "unknown")
        # TODO: decide a better orgnization
        trial_output_dir = (
            experiment_filestore
            / experiment_name
            / crs
            / benchmark
            / harness_name
            / mode
            / f"trial-{trial_num}"
        )
        trial_output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Trial output directory: {trial_output_dir}")

        # Run benchmark evaluation for this specific harness
        # Note: CRS is already configured via executor.configure_crs() above
        result = runner.run_benchmark(
            benchmark_harness=benchmark_harness,
            mode=mode,  # Use mode from trial
            crs_config={},  # Empty config - executor already configured
            trial_output_dir=trial_output_dir,
            oss_fuzz_path=oss_fuzz_path,
        )

        execution_time = time.time() - start_time

        # Create trial metadata
        metadata = TrialMetadata(
            experiment_filestore=config.get("experiment_filestore"),
            max_total_time=config.get("max_total_time"),
            difficulty_level=config.get("difficulty_level"),
            timestamp_start=start_time,
            timestamp_end=time.time(),
        )

        # Create trial result using Pydantic model
        trial_result = TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type_enum,
            success=result.is_valid,
            execution_time=execution_time,
            povs_found=result.povs_found,
            total_povs=result.total_povs,
            patches_generated=result.report.patches_generated,
            patches_valid=result.report.patches_valid,
            report=result.report.to_dict(),
            metadata=metadata,
        )

        # Log completion message
        logger.info(trial_result.log_summary())

        return trial_result

    except FileNotFoundError as e:
        execution_time = time.time() - start_time
        logger.error(f"[Trial {trial_num}] Benchmark not found: {e}")
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type_enum,
            success=False,
            execution_time=execution_time,
            error=f"Benchmark not found: {e!s}",
            error_type="FileNotFoundError",
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
            ),
        )

    except Exception as e:
        execution_time = time.time() - start_time
        # Use str(e) to avoid loguru interpreting curly braces in error messages
        error_msg = str(e).replace("{", "{{").replace("}", "}}")
        logger.error(
            f"[Trial {trial_num}] Failed with error: {error_msg}", exc_info=True
        )
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type_enum,
            success=False,
            execution_time=execution_time,
            error=str(e),
            error_type=type(e).__name__,
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
            ),
        )


def evaluate_crs_trial(trial_id: str, trial_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate and aggregate CRS trial results.

    This job handles post-processing of trial results:
    - POV deduplication
    - Patch validation
    - Result aggregation
    - Report generation

    Args:
        trial_id: Unique trial identifier
        trial_data: Trial execution data from run_crs_trial

    Returns:
        dict: Evaluation results with aggregated data

    Example:
        >>> trial_data = {'crs': 'test', 'benchmark': 'test', 'trial_num': 0}
        >>> result = evaluate_crs_trial('trial-001', trial_data)
        >>> assert result['evaluation_complete'] is True
    """
    logger.info(f"Evaluating trial {trial_id}")

    try:
        # TODO: Implement evaluation logic
        # - POV deduplication using crsbench.deduplication
        # - Patch validation using crsbench.patch_tester
        # - Result aggregation
        # - Statistical analysis

        evaluation_result = {
            "trial_id": trial_id,
            "evaluation_complete": True,
            "crs": trial_data.get("crs"),
            "benchmark": trial_data.get("benchmark"),
            "trial_num": trial_data.get("trial_num"),
            "metadata": {"evaluation_time": 0.0, "timestamp": time.time()},
        }

        logger.info(f"Evaluation completed for trial {trial_id}")
        return evaluation_result

    except Exception as e:
        logger.error(f"Evaluation failed for trial {trial_id}: {e}")
        return {
            "trial_id": trial_id,
            "evaluation_complete": False,
            "error": str(e),
            "metadata": {"timestamp": time.time()},
        }


def _resolve_benchmark_path(benchmark: str, config: Dict[str, Any]) -> Path:
    """
    Resolve benchmark identifier to filesystem path.

    Handles both absolute paths and benchmark identifiers. Searches in standard
    locations if identifier provided.

    Args:
        benchmark: Benchmark identifier or absolute path
        config: Experiment configuration (may contain benchmarks root)

    Returns:
        Path: Resolved benchmark directory path

    Raises:
        FileNotFoundError: If benchmark cannot be found

    Example:
        >>> path = _resolve_benchmark_path('/abs/path/to/bench', {})
        >>> assert path.exists()

        >>> path = _resolve_benchmark_path('test-benchmark', {})
        >>> assert path.name == 'test-benchmark'
    """
    # If it's already an absolute path and exists, use it
    benchmark_path = Path(benchmark)
    if benchmark_path.is_absolute() and benchmark_path.exists():
        logger.debug(f"Using absolute benchmark path: {benchmark_path}")
        return benchmark_path

    # Try to get benchmarks root from config
    benchmarks_root = config.get("benchmarks_root")
    if benchmarks_root:
        benchmark_path = Path(benchmarks_root) / benchmark
        if benchmark_path.exists():
            logger.debug(f"Found benchmark at: {benchmark_path}")
            return benchmark_path

    # Fall back to standard location
    # TODO: Make this configurable or discover dynamically
    default_benchmarks_root = Path(__file__).parent.parent.parent / "benchmarks"
    benchmark_path = default_benchmarks_root / benchmark

    if benchmark_path.exists():
        logger.debug(f"Found benchmark at default location: {benchmark_path}")
        return benchmark_path

    # Benchmark not found
    error_msg = (
        f"Benchmark not found: {benchmark}\n"
        f"Searched in:\n"
        f"  - Absolute path: {Path(benchmark).absolute()}\n"
    )
    if benchmarks_root:
        error_msg += f"  - Config root: {Path(benchmarks_root) / benchmark}\n"
    error_msg += f"  - Default root: {default_benchmarks_root / benchmark}"

    raise FileNotFoundError(error_msg)


def _ensure_project_symlink(
    oss_fuzz_path: Path,
    benchmark_name: str,
    benchmark_path: Path,
) -> None:
    """
    Ensure original project symlink exists in oss-fuzz/projects/.

    Creates a symlink: oss-fuzz/projects/{benchmark_name} -> benchmark_path
    This allows tools like oss-bugfix-crs to find the project.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
        benchmark_name: Name of the benchmark
        benchmark_path: Path to the benchmark directory
    """
    projects_dir = oss_fuzz_path / "projects"
    symlink_path = projects_dir / benchmark_name
    target_path = benchmark_path.resolve()

    # Already exists and correct
    if symlink_path.is_symlink():
        if symlink_path.resolve() == target_path:
            logger.debug(f"Project symlink already exists: {symlink_path}")
            return
        # Wrong target - remove and recreate
        symlink_path.unlink()
    elif symlink_path.exists():
        # It's a real directory, not a symlink - leave it alone
        logger.debug(f"Project directory exists (not symlink): {symlink_path}")
        return

    # Create symlink
    try:
        projects_dir.mkdir(parents=True, exist_ok=True)
        symlink_path.symlink_to(target_path)
        logger.debug(f"Created project symlink: {symlink_path} -> {target_path}")
    except FileExistsError:
        # Race condition - another process created it
        if symlink_path.is_symlink() and symlink_path.resolve() == target_path:
            logger.debug(f"Project symlink created by another process: {symlink_path}")
        else:
            logger.warning(f"Failed to create project symlink: {symlink_path}")
    except Exception as e:
        logger.warning(f"Error creating project symlink: {e}")
