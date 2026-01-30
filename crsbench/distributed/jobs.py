"""Job definitions for CRSBench distributed execution.

This module defines all job types that can be executed by workers in the distributed
job queue system. Jobs are enqueued by the orchestrator and executed by workers.
"""

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from crsbench.evaluation.cleanup import cleanup_trial_directory, copy_trial_results
from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.litellm_tracker import (
    LiteLLMTracker,
    LiteLLMTrackerError,
    is_tracking_available,
)
from crsbench.evaluation.results import CRSType, TrialMetadata, TrialResult
from crsbench.evaluation.runner import BenchmarkFormatError, BenchmarkRunner
from crsbench.utils.crs_helper import get_crs_registry_name
from crsbench.utils.logger import (
    add_file_handler,
    create_trial_filter,
    escape_loguru_braces,
    get_logger,
    remove_file_handler,
    set_trial_context,
)

# Import file-based metadata schema (distinct from evaluation.results.TrialMetadata)
from crsbench.validation.schemas import (
    ExperimentConfig,
    SourceInfo,
    TrialConfig,
    TrialMode,
)
from crsbench.validation.schemas import TrialMetadata as TrialMetadataFile

logger = get_logger(__name__)


def _generate_results_folder_name(
    experiment_name: str, timestamp: Optional[str] = None
) -> str:
    """Generate folder name with experiment name, hostname, and timestamp suffix.

    Format: {experiment_name}_{hostname}_{YYYYMMDD-HHMMSS}

    Args:
        experiment_name: Name of the experiment
        timestamp: Pre-generated timestamp string (format: YYYYMMDD-HHMMSS).
                   If None, generates a new timestamp.

    Returns:
        Folder name with suffix
    """
    hostname = socket.gethostname()
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{experiment_name}_{hostname}_{timestamp}"


def _setup_llm_tracking(
    config: ExperimentConfig,
    crs: str,
    benchmark: str,
    harness_name: str,
    trial_num: int,
    mode: str,
    sanitizer: str,
) -> tuple[Optional[LiteLLMTracker], Optional[str]]:
    """Set up LLM tracking if enabled.

    Args:
        config: Experiment configuration
        crs: CRS name
        benchmark: Benchmark name
        harness_name: Harness name
        trial_num: Trial number
        mode: Build mode
        sanitizer: Sanitizer type

    Returns:
        Tuple of (tracker, api_key) if tracking enabled, (None, None) otherwise
    """
    if not config.llm_tracking_enabled:
        return None, None

    if not is_tracking_available():
        logger.warning(
            "LLM tracking enabled but required env vars not set. "
            "Need (LITELLM_BASE_URL or UPSTREAM_LITELLM_BASE_URL) and "
            "(LITELLM_MASTER_KEY or LITELLM_API_KEY). Skipping tracking."
        )
        return None, None

    try:
        tracker = LiteLLMTracker()

        # Extract cost budget from config if present
        max_budget = None
        if config.resources and config.resources.litellm:
            max_budget = config.resources.litellm.cost_budget

        # Always assign to team: explicit config > experiment name
        team_name = None
        if (
            config.resources
            and config.resources.litellm
            and config.resources.litellm.team
        ):
            team_name = config.resources.litellm.team
        else:
            team_name = config.experiment

        # Extract team budget from config if present
        team_max_budget = None
        if (
            config.resources
            and config.resources.litellm
            and config.resources.litellm.team_max_budget
        ):
            team_max_budget = config.resources.litellm.team_max_budget

        '''
        team_id = tracker.get_or_create_team(team_name, max_budget=team_max_budget)

        # Log team budget information
        try:
            team_info = tracker.get_team_info(team_id)
            team_spend = team_info.get("spend", 0)
            team_budget = team_info.get("max_budget")

            if team_budget is not None:
                remaining = team_budget - team_spend
                logger.info(
                    f"Team '{team_name}' budget: "
                    f"used=${team_spend:.2f}, remaining=${remaining:.2f}, max=${team_budget:.2f}"
                )
            else:
                logger.info(
                    f"Team '{team_name}' budget: used=${team_spend:.2f}, max=unlimited"
                )
        except LiteLLMTrackerError as e:
            logger.warning(f"Could not fetch team budget info: {e}")
        '''

        api_key = tracker.generate_key(
            experiment=config.experiment,
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            mode=mode,
            sanitizer=sanitizer,
            max_budget=max_budget,
        )
        budget_info = f" (budget: ${max_budget})" if max_budget else ""
        logger.info(f"Generated LLM tracking key for trial {trial_num}{budget_info}")

        # Debug: verify budget was actually set by fetching key info
        key_info = tracker.get_key_info(api_key)
        info = key_info.get("info", {})
        actual_budget = info.get("max_budget")
        logger.info(
            f"Key info after creation - max_budget: {actual_budget}, spend: {info.get('spend', 0)}"
        )
        return tracker, api_key
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to set up LLM tracking: {e}")
        return None, None


def _cleanup_llm_tracking(
    tracker: Optional[LiteLLMTracker],
    api_key: Optional[str],
    trial_output_dir: Path,
    trial_id: str,
) -> None:
    """Clean up LLM tracking after trial.

    Args:
        tracker: LiteLLMTracker instance (or None)
        api_key: Trial API key (or None)
        trial_output_dir: Trial output directory
        trial_id: Trial identifier
    """
    if not tracker or not api_key:
        return

    try:
        # Write final usage file (cost/token summary)
        tracker.write_llm_usage_file(
            api_key=api_key,
            trial_id=trial_id,
            output_path=trial_output_dir / "llm-usage.json",
        )
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to write LLM usage file: {e}")

    try:
        # Write detailed LLM logs (all request/response data)
        tracker.write_llm_logs_file(
            api_key=api_key,
            trial_id=trial_id,
            output_path=trial_output_dir / "llm-logs.json",
        )
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to write LLM logs file: {e}")

    try:
        # Delete the key
        tracker.delete_key(api_key)
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to delete LLM tracking key: {e}")


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


def _create_phase_callbacks():
    """Create callbacks for updating job phase metadata in RQ.

    Returns:
        tuple: (on_build_start, on_run_start, on_verification_start) callbacks
    """

    def on_build_start():
        """Update job metadata when CRS build phase starts (idempotent)."""
        try:
            import rq

            job = rq.get_current_job()
            if job:
                # Only set if not already in building phase (idempotent)
                if job.meta.get("phase") != "building":
                    job.meta["phase"] = "building"
                    job.meta["phase_started_at"] = time.time()
                    job.save_meta()
                    logger.debug(f"Job {job.id[:8]} phase -> building")
        except Exception as e:
            logger.warning(f"Failed to update job metadata: {e}")

    def on_run_start():
        """Update job metadata when CRS run phase starts."""
        try:
            import rq

            job = rq.get_current_job()
            if job:
                job.meta["phase"] = "running"
                job.meta["phase_started_at"] = time.time()
                job.save_meta()
                logger.debug(f"Updated job metadata for job {job.id}")
        except Exception as e:
            logger.warning(f"Failed to update job metadata: {e}")

    def on_verification_start():
        """Update job metadata when verification phase starts."""
        try:
            import rq

            job = rq.get_current_job()
            if job:
                job.meta["phase"] = "verifying"
                job.meta["phase_started_at"] = time.time()
                job.save_meta()
                logger.debug(f"Job {job.id[:8]} phase -> verifying")
        except Exception as e:
            logger.warning(f"Failed to update job metadata: {e}")

    return on_build_start, on_run_start, on_verification_start


def _build_trial_output_path(
    filestore: Path,
    experiment_name: str,
    crs: str,
    benchmark: str,
    harness: str,
    mode: str,
    sanitizer: str,
    trial_num: int,
) -> Path:
    """Construct trial output directory path.

    Args:
        filestore: Base filestore directory
        experiment_name: Experiment name
        crs: CRS name
        benchmark: Benchmark name
        harness: Harness name
        mode: Evaluation mode ('delta', 'full', or 'all')
        sanitizer: Sanitizer type ('address', 'memory', or 'undefined')
        trial_num: Trial number

    Returns:
        Path to trial output directory
    """
    return (
        filestore
        / experiment_name
        / crs
        / benchmark
        / harness
        / mode
        / sanitizer
        / f"trial-{trial_num}"
    )


def _reconstruct_trial_result_from_success(
    trial_dir: Path,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
) -> TrialResult:
    """Reconstruct TrialResult from a successful trial's metadata.json.

    Args:
        trial_dir: Path to trial directory
        crs: CRS name
        benchmark: Benchmark name
        harness: Harness name
        trial_num: Trial number

    Returns:
        TrialResult reconstructed from metadata
    """
    metadata_file = trial_dir / "metadata.json"

    # Default values
    crs_type_enum = CRSType.BUG_FINDING
    execution_time = 0.0
    povs_found = 0
    total_povs = 0
    patches_generated = 0
    patches_valid = 0

    if metadata_file.exists():
        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)

            # Extract CRS type from metadata
            mode_str = metadata.get("mode", "bug_finding")
            if mode_str == "patch_generation":
                crs_type_enum = CRSType.BUG_FIXING

            # Try to extract results from metadata if available
            # (These may not be in the standard metadata.json format)
            povs_found = metadata.get("povs_found", 0)
            total_povs = metadata.get("total_povs", 0)
            patches_generated = metadata.get("patches_generated", 0)
            patches_valid = metadata.get("patches_valid", 0)

        except Exception as e:
            logger.warning(
                f"Could not parse metadata.json for trial {trial_num}: {e}. Using defaults."
            )
    else:
        logger.warning(
            f"metadata.json not found for trial {trial_num} at {trial_dir}. Using defaults."
        )

    logger.info(
        f"[Trial {trial_num}] Skipping execution - found existing .success marker at {trial_dir}"
    )

    return TrialResult(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        trial_num=trial_num,
        crs_type=crs_type_enum,
        success=True,
        execution_time=execution_time,
        povs_found=povs_found,
        total_povs=total_povs,
        patches_generated=patches_generated,
        patches_valid=patches_valid,
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
        ),
    )


def _create_failed_trial_result(
    trial_dir: Path,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
) -> TrialResult:
    """Create TrialResult for a previously failed trial.

    Args:
        trial_dir: Path to trial directory
        crs: CRS name
        benchmark: Benchmark name
        harness: Harness name
        trial_num: Trial number

    Returns:
        TrialResult indicating failure (for retry)
    """
    metadata_file = trial_dir / "metadata.json"

    # Default values
    crs_type_enum = CRSType.BUG_FINDING

    if metadata_file.exists():
        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)

            # Extract CRS type from metadata
            mode_str = metadata.get("mode", "bug_finding")
            if mode_str == "patch_generation":
                crs_type_enum = CRSType.BUG_FIXING

        except Exception as e:
            logger.warning(
                f"Could not parse metadata.json for failed trial {trial_num}: {e}. Using default CRS type."
            )

    logger.info(
        f"[Trial {trial_num}] Found existing .fail marker at {trial_dir} - returning failed result for retry"
    )

    return TrialResult(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        trial_num=trial_num,
        crs_type=crs_type_enum,
        success=False,
        execution_time=0.0,
        error="Trial previously failed (marked for retry)",
        error_type="PreviousFailure",
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
        ),
    )


def _check_existing_trial(
    config: ExperimentConfig,
    crs: str,
    benchmark: str,
    harness: str,
    mode: str,
    sanitizer: str,
    trial_num: int,
) -> Optional[TrialResult]:
    """Check for existing trial markers and return TrialResult if found.

    Checks results_filestore first (if configured), then experiment_filestore.
    Returns immediately if .success or .fail marker is found.

    Args:
        config: Experiment configuration
        crs: CRS name
        benchmark: Benchmark name
        harness: Harness name
        mode: Evaluation mode
        sanitizer: Sanitizer type
        trial_num: Trial number

    Returns:
        TrialResult if existing marker found, None otherwise
    """
    experiment_name = config.experiment

    # Check results filestore first (if configured)
    filestores_to_check = []
    if config.results_filestore:
        filestores_to_check.append(config.results_filestore.resolve())
    filestores_to_check.append(config.experiment_filestore.resolve())

    for filestore in filestores_to_check:
        trial_dir = _build_trial_output_path(
            filestore=filestore,
            experiment_name=experiment_name,
            crs=crs,
            benchmark=benchmark,
            harness=harness,
            mode=mode,
            sanitizer=sanitizer,
            trial_num=trial_num,
        )

        if not trial_dir.exists():
            continue

        # Check for success marker
        success_marker = trial_dir / ".success"
        if success_marker.exists():
            return _reconstruct_trial_result_from_success(
                trial_dir=trial_dir,
                crs=crs,
                benchmark=benchmark,
                harness=harness,
                trial_num=trial_num,
            )

        # Check for fail marker
        fail_marker = trial_dir / ".fail"
        if fail_marker.exists():
            return _create_failed_trial_result(
                trial_dir=trial_dir,
                crs=crs,
                benchmark=benchmark,
                harness=harness,
                trial_num=trial_num,
            )

    # No existing trial markers found
    return None


def run_crs_trial(
    crs: str,
    benchmark: str,
    harness_name: str,
    harness_path: str,
    trial_num: int,
    trial_id: str,
    config_dict: Dict[str, Any],
    mode: str,
    sanitizer: str = "address",
    results_timestamp: Optional[str] = None,
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
        trial_id: Pre-generated trial identifier with random suffix
        config_dict: Experiment configuration as dict (deserialized by RQ)
        mode: Evaluation mode ('delta', 'full', or 'all')
        sanitizer: Sanitizer type ('address', 'memory', or 'undefined')

    Returns:
        TrialResult: Trial results including POVs found, success rate, and metadata

    Example:
        >>> from crsbench.validation.schemas import ExperimentConfig
        >>> config = ExperimentConfig(
        ...     experiment_filestore='/tmp/exp',
        ...     max_total_time=3600
        ... )
        >>> result = run_crs_trial(
        ...     'test-crs', 'test-benchmark', 'fuzz_test', '/src/fuzz_test.c', 1,
        ...     config.model_dump(), 'delta'
        ... )
        >>> assert result.povs_found >= 0
    """
    # Reconstruct ExperimentConfig from dict - Pydantic will convert strings to Paths
    config = ExperimentConfig(**config_dict)

    # Check for existing trial markers before any heavy setup
    existing_result = _check_existing_trial(
        config=config,
        crs=crs,
        benchmark=benchmark,
        harness=harness_name,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
    )
    if existing_result is not None:
        return existing_result

    logger.info(
        f"[Trial {trial_num}] Starting CRS '{crs}' on benchmark '{benchmark}' harness '{harness_name}'"
    )
    start_time = time.time()
    crs_type_enum = CRSType.BUG_FINDING  # Default, updated after detection

    # Update runtime job metadata for monitoring (RQ 2.x)
    # Note: Static fields (crs, benchmark, harness, mode, trial_num) are set at enqueue time
    try:
        import rq

        job = rq.get_current_job()
        if job:
            # Only set runtime fields (static fields already set at enqueue)
            job.meta["started_at"] = start_time
            job.meta["worker_name"] = os.environ.get(
                "CRSBENCH_WORKER_DISPLAY_NAME", "unknown"
            )
            job.save_meta()
            logger.debug(f"Updated runtime job metadata for job {job.id}")
    except Exception as e:
        # Don't fail the job if metadata update fails
        logger.warning(f"Failed to update job metadata: {e}")

    try:
        # Get snapshot configuration
        snapshot_period = config.snapshot_period

        # Get allocated_cpus and memory_limit from job metadata
        allocated_cpus = None
        allocated_memory = None
        try:
            import rq

            job = rq.get_current_job()
            if job:
                allocated_cpus = job.meta.get("allocated_cpus")
                allocated_memory = job.meta.get("memory_limit")
                if allocated_cpus:
                    logger.info(f"Job assigned CPUs: {allocated_cpus}")
                if allocated_memory:
                    logger.info(f"Job assigned memory: {allocated_memory}")
        except Exception as e:
            logger.warning(f"Failed to get allocated resources from job metadata: {e}")

        # Fall back to config.resources for local execution (no RQ job)
        if allocated_cpus is None and config.resources:
            allocated_cpus = str(config.resources.cores_per_trial)
            logger.info(f"Using cores_per_trial from config: {allocated_cpus}")
        if allocated_memory is None and config.resources:
            allocated_memory = config.resources.memory_per_trial
            logger.info(f"Using memory_per_trial from config: {allocated_memory}")

        # Helper function to get worker override from environment variable
        def get_worker_override(field: str) -> Optional[str]:
            """Get worker override from environment variable."""
            return os.environ.get(f"CRSBENCH_WORKER_{field.upper()}")

        # Initialize CRS executor
        # Get required paths from config
        # Resolve to absolute paths to avoid issues with relative paths
        # Apply worker overrides if available (via environment variables)
        oss_fuzz_override = get_worker_override("oss_fuzz_path")
        oss_fuzz_path = (
            Path(oss_fuzz_override).resolve()
            if oss_fuzz_override
            else config.oss_fuzz_path.resolve()
        )

        registry_override = get_worker_override("registry_dir")
        registry_dir = (
            Path(registry_override).resolve()
            if registry_override
            else (config.registry_dir or (Path.cwd() / "crses" / "registry")).resolve()
        )

        benchmarks_override = get_worker_override("benchmarks_root")
        benchmarks_root = (
            Path(benchmarks_override).resolve()
            if benchmarks_override
            else (config.benchmarks_root or (Path.cwd() / "benchmarks")).resolve()
        )

        crs_configs_override = get_worker_override("crs_configs_dir")
        crs_configs_dir = (
            Path(crs_configs_override).resolve()
            if crs_configs_override
            else (
                config.crs_configs_dir or (Path.cwd() / "crses" / "configs")
            ).resolve()
        )

        # Also apply overrides to filestore paths (used throughout config)
        experiment_filestore_override = get_worker_override("experiment_filestore")
        if experiment_filestore_override:
            config.experiment_filestore = Path(experiment_filestore_override).resolve()
            logger.info(
                f"Worker override applied: experiment_filestore = {config.experiment_filestore}"
            )

        report_filestore_override = get_worker_override("report_filestore")
        if report_filestore_override:
            config.report_filestore = Path(report_filestore_override).resolve()
            logger.info(
                f"Worker override applied: report_filestore = {config.report_filestore}"
            )

        benchmark_suites_override = get_worker_override("benchmark_suites_root")
        if benchmark_suites_override:
            config.benchmark_suites_root = Path(benchmark_suites_override).resolve()
            logger.info(
                f"Worker override applied: benchmark_suites_root = {config.benchmark_suites_root}"
            )

        # Apply cleanup configuration overrides
        keep_only_results_override = get_worker_override("keep_only_results")
        if keep_only_results_override:
            config.keep_only_results = keep_only_results_override.lower() == "true"
            logger.info(
                f"Worker override applied: keep_only_results = {config.keep_only_results}"
            )

        cleanup_after_trial_override = get_worker_override("cleanup_after_trial")
        if cleanup_after_trial_override:
            config.cleanup_after_trial = cleanup_after_trial_override.lower() == "true"
            logger.info(
                f"Worker override applied: cleanup_after_trial = {config.cleanup_after_trial}"
            )

        copy_results_after_trial_override = get_worker_override(
            "copy_results_after_trial"
        )
        if copy_results_after_trial_override:
            config.copy_results_after_trial = (
                copy_results_after_trial_override.lower() == "true"
            )
            logger.info(
                f"Worker override applied: copy_results_after_trial = {config.copy_results_after_trial}"
            )

        results_filestore_override = get_worker_override("results_filestore")
        if results_filestore_override:
            config.results_filestore = Path(results_filestore_override).resolve()
            logger.info(
                f"Worker override applied: results_filestore = {config.results_filestore}"
            )

        build_workers_override = get_worker_override("build_workers")
        if build_workers_override:
            config.build_workers = int(build_workers_override)
            logger.info(
                f"Worker override applied: build_workers = {config.build_workers}"
            )

        verify_workers_override = get_worker_override("verify_workers")
        if verify_workers_override:
            config.verify_workers = int(verify_workers_override)
            logger.info(
                f"Worker override applied: verify_workers = {config.verify_workers}"
            )

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
                litellm_mode=config.litellm_mode,
            )
        else:
            # Bug finding CRS
            crs_executor = CRSBugFindingExecutor(
                crs_config_name=crs,
                oss_fuzz_path=oss_fuzz_path,
                registry_dir=registry_dir,
                benchmarks_root=benchmarks_root,
                crs_configs_dir=crs_configs_dir,
                litellm_mode=config.litellm_mode,
            )

        # Configure executor
        crs_executor.configure_crs(
            {
                "build_timeout": config.build_timeout,
                "run_timeout": config.run_timeout,
                "hints_enabled": config.hints_enabled,
                "hint_sarif_level": config.hint_sarif_level,
                "hint_corpus_level": config.hint_corpus_level,
                "project_image_prefix": config.project_image_prefix,
                "mode": mode,
                "sanitizer": sanitizer,
                "allocated_cpus": allocated_cpus,
                "allocated_memory": allocated_memory,
                "run_id": trial_id,
                "source_mode": config.source_mode,
            }
        )

        # Initialize benchmark runner with CRS executor and snapshot configuration
        coverage_enabled = config.coverage_enabled
        coverage_saturation_time = config.coverage_saturation_time
        coverage_early_stop = config.coverage_early_stop
        pov_early_stop = config.pov_early_stop
        logger.debug(
            f"Coverage config: enabled={coverage_enabled}, "
            f"saturation_time={coverage_saturation_time}, "
            f"early_stop={coverage_early_stop}, oss_fuzz_path={oss_fuzz_path}"
        )
        logger.debug(f"POV config: early_stop={pov_early_stop}")

        # Create phase callbacks for job metadata tracking
        on_build_start, on_run_start, on_verification_start = _create_phase_callbacks()

        # Set up LLM tracking if enabled (must be before BenchmarkRunner creation)
        llm_tracker, llm_api_key = _setup_llm_tracking(
            config=config,
            crs=crs,
            benchmark=benchmark,
            harness_name=harness_name,
            trial_num=trial_num,
            mode=mode,
            sanitizer=sanitizer,
        )

        # If tracking is enabled, pass the trial-specific API key to executor
        if llm_api_key:
            crs_executor.set_llm_api_key(llm_api_key)
            logger.info("Configured executor with trial-specific LLM API key")

        runner = BenchmarkRunner(
            crs_executor,
            snapshot_period=snapshot_period,
            coverage_enabled=coverage_enabled,
            coverage_saturation_time=coverage_saturation_time,
            coverage_early_stop=coverage_early_stop,
            pov_early_stop=pov_early_stop,
            oss_fuzz_path=oss_fuzz_path,
            on_build_start=on_build_start,
            on_run_start=on_run_start,
            on_verification_start=on_verification_start,
            llm_tracker=llm_tracker,
            llm_api_key=llm_api_key,
            llm_trial_id=trial_id,
            build_workers=config.build_workers,
            verify_workers=config.verify_workers,
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
        experiment_filestore = config.experiment_filestore.resolve()
        experiment_name = config.experiment
        # TODO: decide a better orgnization
        trial_output_dir = _build_trial_output_path(
            filestore=experiment_filestore,
            experiment_name=experiment_name,
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            mode=mode,
            sanitizer=sanitizer,
            trial_num=trial_num,
        )
        trial_output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Trial output directory: {trial_output_dir}")

        # Set up per-trial logging
        set_trial_context(trial_id)
        trial_log_handler = add_file_handler(
            trial_output_dir / "worker.log",
            level="DEBUG",
            filter_func=create_trial_filter(trial_id),
        )
        logger.info(f"Per-trial logging enabled: {trial_output_dir / 'worker.log'}")

        # Write trial metadata.json
        trial_mode = (
            TrialMode.patch_generation
            if crs_type == "bug-fixing"
            else TrialMode.bug_finding
        )
        file_metadata = TrialMetadataFile(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trial_num=trial_num,
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            mode=trial_mode,
            source=SourceInfo(path=str(benchmark_path)),
            config=TrialConfig(
                hints_enabled=config.hints_enabled,
                hints_corpus_level=config.hint_corpus_level,
            ),
        )
        metadata_file = trial_output_dir / "metadata.json"
        with metadata_file.open("w") as f:
            json.dump(file_metadata.model_dump(mode="json"), f, indent=2)
        logger.debug(f"Wrote trial metadata to {metadata_file}")

        # Run benchmark evaluation for this specific harness
        # Note: CRS is already configured via executor.configure_crs() above
        try:
            result = runner.run_benchmark(
                benchmark_harness=benchmark_harness,
                mode=mode,  # Use mode from trial
                crs_config={},  # Empty config - executor already configured
                trial_output_dir=trial_output_dir,
                oss_fuzz_path=oss_fuzz_path,
                skip_verification=config.skip_verification,
            )
        finally:
            # Clean up per-trial logging
            if "trial_log_handler" in locals() and trial_log_handler is not None:
                remove_file_handler(trial_log_handler)
            set_trial_context(None)

            # Clean up LLM tracking (write usage file and delete key)
            _cleanup_llm_tracking(
                tracker=llm_tracker,
                api_key=llm_api_key,
                trial_output_dir=trial_output_dir,
                trial_id=trial_id,
            )

        execution_time = time.time() - start_time

        # Create trial metadata
        metadata = TrialMetadata(
            experiment_filestore=str(config.experiment_filestore),
            max_total_time=config.max_total_time,
            difficulty_level=config.difficulty_level,
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
            success=result.success,
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

        # Create success/fail marker file
        marker_file = trial_output_dir / (".success" if result.success else ".fail")
        marker_file.touch()

        # Per-trial copy if enabled (before cleanup)
        if config.copy_results_after_trial and config.results_filestore:
            experiment_dir = config.experiment_filestore.resolve() / config.experiment
            rel_path = trial_output_dir.relative_to(experiment_dir)
            folder_name = _generate_results_folder_name(
                config.experiment, results_timestamp
            )
            dest_dir = (
                Path(config.results_filestore)
                / folder_name
                / "experiment-data"
                / rel_path
            )
            copy_trial_results(trial_output_dir, dest_dir)

        # Per-trial cleanup if enabled
        if config.cleanup_after_trial:
            cleanup_trial_directory(trial_output_dir)

        return trial_result

    except FileNotFoundError as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.error(f"[Trial {trial_num}] Benchmark not found: {error_msg}")
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            (trial_output_dir / ".fail").touch()
            # Per-trial copy if enabled (before cleanup, even for failed trials)
            if config.copy_results_after_trial and config.results_filestore:
                experiment_dir = (
                    config.experiment_filestore.resolve() / config.experiment
                )
                rel_path = trial_output_dir.relative_to(experiment_dir)
                folder_name = _generate_results_folder_name(
                    config.experiment, results_timestamp
                )
                dest_dir = (
                    Path(config.results_filestore)
                    / folder_name
                    / "experiment-data"
                    / rel_path
                )
                copy_trial_results(trial_output_dir, dest_dir)
            # Per-trial cleanup if enabled (even for failed trials)
            if config.cleanup_after_trial:
                cleanup_trial_directory(trial_output_dir)
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

    except BenchmarkFormatError as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.error(f"[Trial {trial_num}] Invalid benchmark format: {error_msg}")
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            (trial_output_dir / ".fail").touch()
            # Per-trial copy if enabled (before cleanup, even for failed trials)
            if config.copy_results_after_trial and config.results_filestore:
                experiment_dir = (
                    config.experiment_filestore.resolve() / config.experiment
                )
                rel_path = trial_output_dir.relative_to(experiment_dir)
                folder_name = _generate_results_folder_name(
                    config.experiment, results_timestamp
                )
                dest_dir = (
                    Path(config.results_filestore)
                    / folder_name
                    / "experiment-data"
                    / rel_path
                )
                copy_trial_results(trial_output_dir, dest_dir)
            # Per-trial cleanup if enabled (even for failed trials)
            if config.cleanup_after_trial:
                cleanup_trial_directory(trial_output_dir)
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type_enum,
            success=False,
            execution_time=execution_time,
            error=f"Invalid benchmark format: {e!s}",
            error_type="BenchmarkFormatError",
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
            ),
        )

    except Exception as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.error(
            f"[Trial {trial_num}] Failed with error: {error_msg}", exc_info=True
        )
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            (trial_output_dir / ".fail").touch()
            # Per-trial copy if enabled (before cleanup, even for failed trials)
            if config.copy_results_after_trial and config.results_filestore:
                experiment_dir = (
                    config.experiment_filestore.resolve() / config.experiment
                )
                rel_path = trial_output_dir.relative_to(experiment_dir)
                folder_name = _generate_results_folder_name(
                    config.experiment, results_timestamp
                )
                dest_dir = (
                    Path(config.results_filestore)
                    / folder_name
                    / "experiment-data"
                    / rel_path
                )
                copy_trial_results(trial_output_dir, dest_dir)
            # Per-trial cleanup if enabled (even for failed trials)
            if config.cleanup_after_trial:
                cleanup_trial_directory(trial_output_dir)
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


def _resolve_benchmark_path(benchmark: str, config: ExperimentConfig) -> Path:
    """
    Resolve benchmark identifier to filesystem path.

    Handles both absolute paths and benchmark identifiers. Searches in standard
    locations if identifier provided.

    Args:
        benchmark: Benchmark identifier or absolute path
        config: Experiment configuration

    Returns:
        Path: Resolved benchmark directory path

    Raises:
        FileNotFoundError: If benchmark cannot be found

    Example:
        >>> from crsbench.validation.schemas import ExperimentConfig
        >>> config = ExperimentConfig()
        >>> path = _resolve_benchmark_path('/abs/path/to/bench', config)
        >>> assert path.exists()

        >>> path = _resolve_benchmark_path('test-benchmark', config)
        >>> assert path.name == 'test-benchmark'
    """
    # If it's already an absolute path and exists, use it
    benchmark_path = Path(benchmark)
    if benchmark_path.is_absolute() and benchmark_path.exists():
        logger.debug(f"Using absolute benchmark path: {benchmark_path}")
        return benchmark_path

    # Try to get benchmarks root from config
    benchmarks_root = config.benchmarks_root
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
