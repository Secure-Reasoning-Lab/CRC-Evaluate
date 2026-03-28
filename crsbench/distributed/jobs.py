"""Job definitions for CRSBench distributed execution.

This module defines all job types that can be executed by workers in the distributed
job queue system. Jobs are enqueued by the orchestrator and executed by workers.
"""

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, cast

import yaml

from crsbench.distributed.job_lifecycle import (
    JobLifecycleRecord,
    JobLifecycleStore,
    JobState,
    LifecycleRedisProtocol,
)
from crsbench.distributed.queue import build_trial_key
from crsbench.evaluation.adapter import create_adapter
from crsbench.evaluation.cleanup import cleanup_trial_directory, copy_trial_results
from crsbench.evaluation.litellm_tracker import (
    LiteLLMTracker,
    LiteLLMTrackerError,
)
from crsbench.evaluation.results import TrialMetadata, TrialResult
from crsbench.evaluation.runner import BenchmarkFormatError, BenchmarkRunner
from crsbench.evaluation.trial_paths import (
    experiment_dir as resolve_experiment_dir,
)
from crsbench.evaluation.trial_paths import (
    trial_relative_to_experiment,
)
from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)
from crsbench.utils.logger import (
    add_file_handler,
    create_trial_filter,
    escape_loguru_braces,
    get_logger,
    remove_file_handler,
    set_trial_context,
)
from crsbench.utils.run_helper import ensure_oss_fuzz_root
from crsbench.utils.storage_warning import warn_for_persisted_storage_roots

# Import file-based metadata schema (distinct from evaluation.results.TrialMetadata)
from crsbench.validation.schemas import (
    ExperimentConfig,
    SourceInfo,
    TrialConfig,
    TrialMode,
)
from crsbench.validation.schemas import TrialMetadata as TrialMetadataFile

logger = get_logger(__name__)


@dataclass(frozen=True)
class JobLifecycleRuntime:
    """Bound lifecycle state access for the current RQ trial job."""

    experiment_name: str
    job_id: str
    worker_name: str
    store: JobLifecycleStore


@dataclass(frozen=True)
class EffectiveInputSettings:
    """Runtime input settings derived from explicit inputs contract."""

    pov_enabled: bool
    max_pov_variants_per_cpv: Optional[int]
    hints_enabled: bool
    hint_sarif_level: Optional[int]
    hint_corpus_level: Optional[int]
    seed_corpus_enabled: bool
    seed_corpus_max_time: Optional[int]
    diff_enabled: bool
    patch_verify_variants: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings for metadata/job payloads."""
        return {
            "pov": {
                "enabled": self.pov_enabled,
                "max_variants_per_cpv": self.max_pov_variants_per_cpv,
            },
            "sarif": {
                "enabled": self.hints_enabled,
                "level": self.hint_sarif_level,
            },
            "seed": {
                "enabled": self.seed_corpus_enabled,
                "max_time": self.seed_corpus_max_time,
            },
            "diff": {"enabled": self.diff_enabled},
            "patch_verify_variants": self.patch_verify_variants,
        }


def _resolve_effective_input_settings(
    config: ExperimentConfig,
    raw_config: Dict[str, Any],
    *,
    trial_mode: Optional[str] = None,
) -> EffectiveInputSettings:
    """Resolve effective runtime inputs from explicit runtime.inputs only.

    Args:
        config: Parsed experiment configuration.
        raw_config: Raw config dict (unused, kept for signature compat).
        trial_mode: Per-trial evaluation mode ("delta" or "full").
            When "delta", diff input is force-enabled because the reference
            diff is a definitional input for delta-mode evaluation.
    """
    del raw_config
    inputs = config.inputs

    pov_enabled = bool(inputs.pov.enabled)
    max_pov_variants = inputs.pov.max_variants_per_cpv
    hints_enabled = bool(inputs.sarif.enabled)
    hint_sarif_level = inputs.sarif.level
    seed_corpus_enabled = bool(inputs.seed.enabled)
    seed_corpus_max_time = inputs.seed.max_time
    diff_enabled = bool(inputs.diff.enabled)
    hint_corpus_level = None

    # Delta mode requires the reference diff by definition.
    if trial_mode == "delta" and not diff_enabled:
        diff_enabled = True
        logger.info(
            "Auto-enabling diff input for delta-mode trial "
            "(ref.diff is a definitional input for delta evaluation)"
        )

    return EffectiveInputSettings(
        pov_enabled=pov_enabled,
        max_pov_variants_per_cpv=max_pov_variants if pov_enabled else None,
        hints_enabled=hints_enabled,
        hint_sarif_level=hint_sarif_level if hints_enabled else None,
        hint_corpus_level=hint_corpus_level if hints_enabled else None,
        seed_corpus_enabled=seed_corpus_enabled,
        seed_corpus_max_time=seed_corpus_max_time if seed_corpus_enabled else None,
        diff_enabled=diff_enabled,
        patch_verify_variants=config.patch_verify_variants,
        source="inputs",
    )


def _resolve_redis_host(config: ExperimentConfig) -> Optional[str]:
    """Resolve the Redis host for verify queue connections.

    The config.redis_host may be 'localhost' because the orchestrator serialized
    it from its own perspective. On remote workers, we need the actual Redis host.
    Priority: RQ job connection > config.redis_host.
    """
    try:
        import rq

        job = rq.get_current_job()
        if job and job.connection:
            conn_kwargs = job.connection.connection_pool.connection_kwargs
            host = conn_kwargs.get("host", "localhost")
            if host != "localhost":
                return host
    except ImportError:
        logger.debug("RQ unavailable while resolving redis host; using config value")
    except Exception as e:
        logger.warning(f"Failed to resolve redis host from current RQ job context: {e}")
    return getattr(config, "redis_host", None)


def _load_benchmark_language(benchmark_path: Path) -> str:
    """Load benchmark language from project.yaml for oss-crs env wiring.

    Returns "c" when project.yaml is missing/invalid to preserve prior behavior.
    """
    project_yaml = benchmark_path / "project.yaml"
    if not project_yaml.exists():
        logger.warning(
            f"project.yaml not found for benchmark {benchmark_path}; "
            "defaulting FUZZING_LANGUAGE=c"
        )
        return "c"

    try:
        with project_yaml.open() as f:
            project = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(
            f"Failed to read {project_yaml}: {e}; defaulting FUZZING_LANGUAGE=c"
        )
        return "c"

    language = str(project.get("language", "c")).strip()
    if not language:
        logger.warning(
            f"Empty language in {project_yaml}; defaulting FUZZING_LANGUAGE=c"
        )
        return "c"
    return language


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


def _initialize_job_lifecycle_runtime(
    *,
    config: ExperimentConfig,
    trial_key: str,
    runtime_worker_name: str,
) -> JobLifecycleRuntime | None:
    """Bind the current RQ job to the shadow lifecycle store and mark it running."""
    try:
        import rq

        job = rq.get_current_job()
        if job is None or job.connection is None or not job.id:
            return None

        store = JobLifecycleStore(cast("LifecycleRedisProtocol", job.connection))
        runtime = JobLifecycleRuntime(
            experiment_name=config.experiment,
            job_id=job.id,
            worker_name=runtime_worker_name,
            store=store,
        )

        record = store.get(runtime.experiment_name, runtime.job_id)
        if record is None:
            store.set(
                runtime.experiment_name,
                JobLifecycleRecord(
                    job_id=runtime.job_id,
                    trial_key=trial_key,
                    state=JobState.QUEUED,
                    claimed_by=None,
                ),
            )
            record = store.get(runtime.experiment_name, runtime.job_id)

        if record is not None and record.state is JobState.QUEUED:
            record = store.transition(
                runtime.experiment_name,
                runtime.job_id,
                JobState.CLAIMED,
                claimed_by=runtime_worker_name,
            )
        if record is not None and record.state is JobState.CLAIMED:
            store.transition(
                runtime.experiment_name,
                runtime.job_id,
                JobState.RUNNING,
                claimed_by=runtime_worker_name,
            )
        _update_job_lifecycle_heartbeat(runtime)
        return runtime
    except Exception as exc:
        logger.warning(f"Failed to initialize job lifecycle tracking: {exc}")
        return None


def _update_job_lifecycle_heartbeat(runtime: JobLifecycleRuntime | None) -> None:
    """Best-effort lifecycle heartbeat update."""
    if runtime is None:
        return
    try:
        if not _lifecycle_runtime_is_current_owner(runtime):
            return
        runtime.store.update_heartbeat(runtime.experiment_name, runtime.job_id)
    except Exception as exc:
        logger.warning(f"Failed to update job lifecycle heartbeat: {exc}")


def _lifecycle_runtime_is_current_owner(runtime: JobLifecycleRuntime | None) -> bool:
    """Return whether the current worker still owns lifecycle writes for this job."""
    if runtime is None:
        return True

    try:
        record = runtime.store.get(runtime.experiment_name, runtime.job_id)
    except Exception as exc:
        logger.warning(f"Failed to read job lifecycle ownership: {exc}")
        return False

    if record is None:
        return False
    return (
        record.state in {JobState.CLAIMED, JobState.RUNNING, JobState.SYNCING}
        and record.claimed_by == runtime.worker_name
    )


def _transition_job_lifecycle_syncing(runtime: JobLifecycleRuntime | None) -> None:
    """Move a running lifecycle record into SYNCING when output collection begins."""
    if runtime is None:
        return
    try:
        record = runtime.store.get(runtime.experiment_name, runtime.job_id)
        if record is None:
            return
        if record.claimed_by != runtime.worker_name:
            return
        if record.state is JobState.RUNNING:
            runtime.store.transition(
                runtime.experiment_name,
                runtime.job_id,
                JobState.SYNCING,
            )
        _update_job_lifecycle_heartbeat(runtime)
    except Exception as exc:
        logger.warning(f"Failed to transition job lifecycle to syncing: {exc}")


def _finish_job_lifecycle(
    runtime: JobLifecycleRuntime | None,
    *,
    success: bool,
    detail: str | None = None,
) -> None:
    """Best-effort terminal lifecycle update for the current job."""
    if runtime is None:
        return
    try:
        record = runtime.store.get(runtime.experiment_name, runtime.job_id)
        if record is None:
            return
        if record.claimed_by != runtime.worker_name:
            logger.warning(
                "Skipping lifecycle finalization for superseded worker %s on job %s",
                runtime.worker_name,
                runtime.job_id,
            )
            return
        if success:
            if record.state is JobState.RUNNING:
                record = runtime.store.transition(
                    runtime.experiment_name,
                    runtime.job_id,
                    JobState.SYNCING,
                )
            if record.state is JobState.CLAIMED:
                record = runtime.store.transition(
                    runtime.experiment_name,
                    runtime.job_id,
                    JobState.RUNNING,
                )
                record = runtime.store.transition(
                    runtime.experiment_name,
                    runtime.job_id,
                    JobState.SYNCING,
                )
            if record.state is JobState.SYNCING:
                runtime.store.transition(
                    runtime.experiment_name,
                    runtime.job_id,
                    JobState.COMPLETED,
                    detail=detail,
                )
            return

        if record.state in {
            JobState.QUEUED,
            JobState.CLAIMED,
            JobState.RUNNING,
            JobState.SYNCING,
            JobState.ORPHANED,
        }:
            runtime.store.transition(
                runtime.experiment_name,
                runtime.job_id,
                JobState.FAILED,
                detail=detail,
            )
    except Exception as exc:
        logger.warning(f"Failed to finalize job lifecycle state: {exc}")


def _publish_trial_terminal_artifacts(
    *,
    config: ExperimentConfig,
    trial_output_dir: Path,
    success: bool,
    results_timestamp: str | None,
    lifecycle_runtime: JobLifecycleRuntime | None,
    metadata_writer: Callable[[], None] | None = None,
) -> bool:
    """Publish terminal worker-side artifacts only for the current lifecycle owner."""
    if not _lifecycle_runtime_is_current_owner(lifecycle_runtime):
        if lifecycle_runtime is not None:
            logger.warning(
                "Skipping terminal artifact publication for superseded worker %s on job %s",
                lifecycle_runtime.worker_name,
                lifecycle_runtime.job_id,
            )
        return False

    marker_file = trial_output_dir / (".success" if success else ".fail")
    opposite_marker = trial_output_dir / (".fail" if success else ".success")
    if opposite_marker.exists() and not marker_file.exists():
        logger.warning(
            "Skipping conflicting worker terminal update for %s; canonical marker %s already exists",
            trial_output_dir,
            opposite_marker.name,
        )
        return False

    if metadata_writer is not None:
        metadata_writer()
    if opposite_marker.exists():
        opposite_marker.unlink()
    if not marker_file.exists():
        marker_file.touch()

    if config.copy_results_after_trial and config.results_filestore:
        experiment_dir = resolve_experiment_dir(
            config.experiment_filestore.resolve(), config.experiment
        )
        rel_path = trial_relative_to_experiment(trial_output_dir, experiment_dir)
        folder_name = _generate_results_folder_name(
            config.experiment, results_timestamp
        )
        dest_dir = (
            Path(config.results_filestore) / folder_name / "experiment-data" / rel_path
        )
        copy_trial_results(trial_output_dir, dest_dir)

    if config.cleanup_after_trial:
        cleanup_trial_directory(trial_output_dir)

    return True


def _start_job_lifecycle_heartbeat(
    runtime: JobLifecycleRuntime | None,
    *,
    interval_seconds: float = 60.0,
) -> tuple[threading.Event, threading.Thread] | None:
    """Start a background heartbeat loop for long-running distributed jobs."""
    if runtime is None:
        return None

    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            _update_job_lifecycle_heartbeat(runtime)

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"job-heartbeat-{runtime.job_id[:12]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _stop_job_lifecycle_heartbeat(
    heartbeat_runtime: tuple[threading.Event, threading.Thread] | None,
) -> None:
    """Stop and join a background heartbeat loop."""
    if heartbeat_runtime is None:
        return
    stop_event, thread = heartbeat_runtime
    stop_event.set()
    thread.join(timeout=2.0)


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

    runtime_env = resolve_litellm_runtime_env(config.litellm_mode or "external")
    contract_errors = required_env_errors_for_mode(runtime_env, tracking_enabled=True)
    if contract_errors:
        joined = "; ".join(contract_errors)
        logger.warning(
            "LLM tracking enabled but canonical runtime inputs are missing. "
            f"{joined}. Skipping tracking."
        )
        return None, None

    try:
        tracker = LiteLLMTracker(
            base_url=runtime_env.tracking_base_url,
            master_key=runtime_env.master_key,
        )

        # Extract cost budget from runtime LiteLLM config if present
        max_budget = config.litellm_cost_budget

        # TODO(team-tracking): Re-enable team budget tracking once LiteLLM team API is integrated

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


def _cleanup_llm_tracking_sync(
    tracker: LiteLLMTracker,
    api_key: str,
    trial_output_dir: Path,
    trial_id: str,
) -> None:
    """Synchronous LLM tracking cleanup (runs in background thread).

    Writes usage/logs/summary files and deletes the API key.
    Each call uses the tracker's built-in retry+backoff, so transient
    failures are handled without losing data.
    """
    try:
        tracker.write_llm_usage_file(
            api_key=api_key,
            trial_id=trial_id,
            output_path=trial_output_dir / "llm-usage.json",
        )
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to write LLM usage file: {e}")

    try:
        tracker.write_llm_logs_file(
            api_key=api_key,
            trial_id=trial_id,
            output_path=trial_output_dir / "llm-logs.json",
        )
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to write LLM logs file: {e}")

    try:
        tracker.write_llm_summary_file(
            api_key=api_key,
            trial_id=trial_id,
            output_path=trial_output_dir / "llm-summary.json",
        )
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to write LLM summary file: {e}")

    try:
        tracker.delete_key(api_key)
    except LiteLLMTrackerError as e:
        logger.error(f"Failed to delete LLM tracking key: {e}")


def _cleanup_llm_tracking(
    tracker: Optional[LiteLLMTracker],
    api_key: Optional[str],
    trial_output_dir: Path,
    trial_id: str,
) -> None:
    """Clean up LLM tracking after trial in a background thread.

    Launches the tracking cleanup asynchronously so the worker can
    immediately pick up the next job.  The background thread uses
    retry+backoff (via the tracker) to handle transient API failures.

    The thread is non-daemon so it survives worker shutdown and can
    finish writing data. Previous cleanup threads are joined with a
    short timeout before starting a new one to bound accumulation.

    Args:
        tracker: LiteLLMTracker instance (or None)
        api_key: Trial API key (or None)
        trial_output_dir: Trial output directory
        trial_id: Trial identifier
    """
    if not tracker or not api_key:
        return

    import threading

    # Join any previous cleanup thread (non-blocking — just reap if done)
    prev = getattr(_cleanup_llm_tracking, "_prev_thread", None)
    if prev is not None and prev.is_alive():
        prev.join(timeout=1.0)

    thread = threading.Thread(
        target=_cleanup_llm_tracking_sync,
        args=(tracker, api_key, trial_output_dir, trial_id),
        name=f"llm-cleanup-{trial_id[:30]}",
        daemon=False,
    )
    thread.start()
    _cleanup_llm_tracking._prev_thread = thread  # type: ignore[attr-defined]
    logger.debug(f"LLM tracking cleanup started in background thread: {thread.name}")


def get_crs_type(crs_name: str, registry_dir: Path) -> str:
    """Read CRS type from registry entry.

    Expected registry layout:
    ``<registry_dir>/<crs_name>.yaml`` (oss-crs format)

    Args:
        crs_name: Name of the CRS
        registry_dir: Path to CRS registry directory

    Returns:
        CRS type: 'bug-finding' or 'bug-fixing'

    Raises:
        FileNotFoundError: If registry entry not found
        ValueError: If type field is missing or invalid
    """
    oss_crs_yaml = registry_dir / f"{crs_name}.yaml"
    if not oss_crs_yaml.exists():
        raise FileNotFoundError(
            f"CRS registry entry not found for '{crs_name}' in {registry_dir}. "
            f"Expected: {oss_crs_yaml}"
        )

    with oss_crs_yaml.open("r") as f:
        pkg_data = yaml.safe_load(f)

    crs_type = pkg_data.get("type")
    if not crs_type:
        raise ValueError(f"CRS type not specified in {oss_crs_yaml}")

    # oss-crs registry uses list form: type: [bug-finding]
    if isinstance(crs_type, list):
        if len(crs_type) != 1:
            raise ValueError(
                f"Invalid CRS type list in {oss_crs_yaml}: {crs_type}. "
                "Expected exactly one CRS type."
            )
        crs_type = crs_type[0]

    if crs_type not in ["bug-finding", "bug-fixing"]:
        raise ValueError(
            f"Invalid CRS type '{crs_type}' in {oss_crs_yaml}. "
            "Must be 'bug-finding' or 'bug-fixing'"
        )

    return str(crs_type)


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


def _create_phase_callbacks(lifecycle_runtime: JobLifecycleRuntime | None = None):
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
        _update_job_lifecycle_heartbeat(lifecycle_runtime)

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
        _update_job_lifecycle_heartbeat(lifecycle_runtime)

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
        _transition_job_lifecycle_syncing(lifecycle_runtime)

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
    target_cpv_id: str | None = None,
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
    parts = [filestore, experiment_name, crs, benchmark, harness]
    if target_cpv_id:
        parts.append(target_cpv_id)
    parts.extend([mode, sanitizer, f"trial-{trial_num}"])
    return Path(*parts)


def _reconstruct_trial_result_from_success(
    trial_dir: Path,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
    target_cpv_id: str | None = None,
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
    crs_type = "bug-finding"
    execution_time = 0.0
    povs_found = 0
    total_povs = 0
    patches_generated = 0
    patches_verified = 0
    patches_valid = 0
    build_time = None
    run_time = None

    if metadata_file.exists():
        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)

            # Extract CRS type from metadata
            mode_str = metadata.get("mode", "bug_finding")
            if mode_str == "patch_generation":
                crs_type = "bug-fixing"

            # Try to extract results from metadata if available
            # (These may not be in the standard metadata.json format)
            povs_found = metadata.get("povs_found", 0)
            total_povs = metadata.get("total_povs", 0)
            patches_generated = metadata.get("patches_generated", 0)
            patches_verified = metadata.get("patches_verified", 0)
            patches_valid = metadata.get("patches_valid", 0)
            build_time = metadata.get("build_time")
            run_time = metadata.get("run_time")

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
        crs_type=crs_type,
        mode=None,
        sanitizer=None,
        target_cpv_id=target_cpv_id,
        success=True,
        execution_time=execution_time,
        povs_found=povs_found,
        total_povs=total_povs,
        patches_generated=patches_generated,
        patches_verified=patches_verified,
        patches_valid=patches_valid,
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
            build_time=build_time,
            run_time=run_time,
            target_cpv_id=target_cpv_id,
        ),
    )


def _create_failed_trial_result(
    trial_dir: Path,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
    target_cpv_id: str | None = None,
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
    crs_type = "bug-finding"

    if metadata_file.exists():
        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)

            # Extract CRS type from metadata
            mode_str = metadata.get("mode", "bug_finding")
            if mode_str == "patch_generation":
                crs_type = "bug-fixing"

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
        crs_type=crs_type,
        mode=None,
        sanitizer=None,
        target_cpv_id=target_cpv_id,
        success=False,
        execution_time=0.0,
        error="Trial previously failed (marked for retry)",
        error_type="PreviousFailure",
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
            target_cpv_id=target_cpv_id,
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
    target_cpv_id: str | None = None,
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
            target_cpv_id=target_cpv_id,
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
                target_cpv_id=target_cpv_id,
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
                target_cpv_id=target_cpv_id,
            )

    # No existing trial markers found
    return None


def _apply_worker_overrides(config: ExperimentConfig) -> None:
    """Apply worker config overrides to the experiment config.

    In distributed execution, the orchestrator serializes the full
    ExperimentConfig (via ``config.model_dump()``) into each Redis job.
    Workers on different machines deserialize this config, but the paths
    (benchmarks_root, etc.) reflect the orchestrator's
    filesystem — which may differ from the worker's.

    The ``worker:`` section in the experiment YAML provides
    machine-specific overrides for workers. Since this section is
    included in the serialized config, workers can apply it at
    deserialization time to fix paths for their local filesystem.

    Note: All workers sharing the same config get the same overrides.
    For heterogeneous clusters where each worker needs different paths,
    use shared storage with consistent mount points instead.
    """
    wc = config.worker
    if wc is not None:
        path_fields = ["registry_dir", "benchmarks_root", "benchmark_suites_root"]
        for field in path_fields:
            value = getattr(wc, field, None)
            if value is not None:
                setattr(config, field, value)
                logger.info(f"Worker override applied: {field} = {value}")

        storage_overrides = wc.storage
        if storage_overrides:
            storage_path_fields = [
                "experiment_filestore",
                "report_filestore",
                "results_filestore",
            ]
            for field in storage_path_fields:
                value = getattr(storage_overrides, field, None)
                if value is not None:
                    setattr(config, field, value)
                    logger.info(f"Worker storage override applied: {field} = {value}")

        if storage_overrides:
            storage_bool_fields = [
                "keep_only_results",
                "cleanup_after_trial",
                "copy_results_after_trial",
            ]
            for field in storage_bool_fields:
                value = getattr(storage_overrides, field, None)
                if value is not None:
                    setattr(config, field, value)
                    logger.info(f"Worker storage override applied: {field} = {value}")

    warn_for_persisted_storage_roots(
        experiment_filestore=config.experiment_filestore,
        report_filestore=config.report_filestore,
        copy_results_after_trial=config.copy_results_after_trial,
        results_filestore=config.results_filestore,
    )


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
    target_cpv_id: str | None = None,
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
        target_cpv_id: Target CPV ID for per-CPV bug-fixing trials

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
    # Reconstruct ExperimentConfig from dict - Pydantic will convert strings to Paths.
    # Strip internal transport-only markers before schema validation.
    config_parse_dict = dict(config_dict)
    config = ExperimentConfig(**config_parse_dict)
    effective_inputs = _resolve_effective_input_settings(
        config, config_dict, trial_mode=mode
    )
    trial_key = build_trial_key(
        crs=crs,
        benchmark=benchmark,
        harness=harness_name,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )

    # Apply worker overrides from config.worker section (if present)
    _apply_worker_overrides(config)

    # Check for existing trial markers before any heavy setup
    existing_result = _check_existing_trial(
        config=config,
        crs=crs,
        benchmark=benchmark,
        harness=harness_name,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )
    if existing_result is not None:
        return existing_result

    logger.info(
        f"[Trial {trial_num}] Starting CRS '{crs}' on benchmark '{benchmark}' harness '{harness_name}'"
    )
    start_time = time.time()
    crs_type = "bug-finding"  # Default, updated after detection
    runtime_worker_name = (
        os.environ.get("CRSBENCH_WORKER_DISPLAY_NAME") or socket.gethostname()
    )
    lifecycle_runtime = _initialize_job_lifecycle_runtime(
        config=config,
        trial_key=trial_key,
        runtime_worker_name=runtime_worker_name,
    )
    heartbeat_runtime = _start_job_lifecycle_heartbeat(lifecycle_runtime)

    # Update runtime job metadata for monitoring (RQ 2.x)
    # Note: Static fields (crs, benchmark, harness, mode, trial_num) are set at enqueue time
    try:
        import rq

        job = rq.get_current_job()
        if job:
            # Only set runtime fields (static fields already set at enqueue)
            job.meta["started_at"] = start_time
            job.meta["worker_name"] = runtime_worker_name
            job.meta["effective_inputs"] = effective_inputs.to_dict()
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
        # Note: In local mode, we use CPUs 0 to N-1 (e.g., cores_per_trial=16 -> cpuset "0-15").
        # In distributed mode, workers allocate specific CPU cores via job metadata.
        if (
            allocated_cpus is None
            and config.resources
            and config.resources.cores_per_trial is not None
        ):
            # Convert core count to cpuset range (e.g., 16 -> "0-15")
            from crsbench.utils.cpu_pool import format_cpuset

            cores = config.resources.cores_per_trial
            allocated_cpus = format_cpuset(list(range(cores)))
            logger.info(
                f"Using cores_per_trial from config: {cores} cores -> cpuset {allocated_cpus}"
            )
        if allocated_memory is None and config.resources:
            allocated_memory = config.resources.memory_per_trial
            if allocated_memory:
                logger.info(f"Using memory_per_trial from config: {allocated_memory}")

        # Determine whether to create a local cgroup for this trial.
        # If ci_supervisor already created a cgroup (distributed mode),
        # job.meta will have "cgroup_parent" -- skip local creation.
        # Otherwise, always attempt local cgroup (ResourceContext handles
        # graceful degradation internally).
        use_local_cgroup = True
        try:
            import rq as _rq

            _current_job = _rq.get_current_job()
            if _current_job and _current_job.meta.get("cgroup_parent"):
                use_local_cgroup = False
                logger.info(
                    "Supervisor cgroup detected, skipping local cgroup creation"
                )
        except Exception:
            pass

        # Get required paths from config
        # Resolve to absolute paths to avoid issues with relative paths
        oss_fuzz_path = Path(ensure_oss_fuzz_root())
        registry_dir = config.registry_dir.resolve()
        benchmarks_root = config.benchmarks_root.resolve()
        registry_name = crs
        logger.info(f"Using CRS registry entry '{registry_name}'")

        # Detect CRS type from registry
        crs_type = get_crs_type(registry_name, registry_dir)
        logger.info(f"Detected CRS type '{crs_type}' for CRS '{crs}'")

        # Create adapter
        adapter = create_adapter(
            config=config,
            crs_config_name=crs,
            oss_fuzz_path=oss_fuzz_path,
            registry_dir=registry_dir,
            benchmarks_root=benchmarks_root,
            mode=crs_type,
        )

        # Resolve benchmark path and language for adapter env wiring
        benchmark_path = _resolve_benchmark_path(benchmark, config)
        benchmark_language = _load_benchmark_language(benchmark_path)
        logger.debug(f"Resolved benchmark path: {benchmark_path}")

        # Configure adapter
        compose_config = {}
        if config.crs_compose:
            compose_config = config.crs_compose.model_dump(exclude={"services"})
            compose_config.update(
                {
                    name: service.model_dump()
                    for name, service in config.crs_compose.services.items()
                }
            )

        adapter_config = {
            "build_timeout": config.build_timeout,
            "run_timeout": config.run_timeout,
            "hints_enabled": effective_inputs.hints_enabled,
            "hint_sarif_level": effective_inputs.hint_sarif_level,
            "hint_corpus_level": effective_inputs.hint_corpus_level,
            "seed_corpus_enabled": effective_inputs.seed_corpus_enabled,
            "seed_corpus_max_time": effective_inputs.seed_corpus_max_time,
            "diff_input_enabled": effective_inputs.diff_enabled,
            "pov_input_enabled": effective_inputs.pov_enabled,
            "max_pov_variants_per_cpv": effective_inputs.max_pov_variants_per_cpv,
            "patch_verify_variants": effective_inputs.patch_verify_variants,
            "inputs": effective_inputs.to_dict(),
            "project_image_prefix": config.project_image_prefix,
            "mode": mode,
            "sanitizer": sanitizer,
            "allocated_cpus": allocated_cpus,
            "allocated_memory": allocated_memory,
            "run_id": trial_id,
            "source_mode": config.source_mode,
            "skip_litellm": config.skip_litellm,
            **compose_config,
        }
        adapter_config["fuzzing_language"] = benchmark_language
        adapter.configure(adapter_config)

        # Initialize benchmark runner with adapter and snapshot configuration
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
        on_build_start, on_run_start, on_verification_start = _create_phase_callbacks(
            lifecycle_runtime
        )

        # Set up LLM tracking/runtime only when LiteLLM is enabled for this trial.
        llm_tracker = None
        llm_api_key = None
        if not config.skip_litellm:
            os.environ["CRSBENCH_LLM_MODE"] = config.litellm_mode or "external"
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
                set_key = getattr(adapter, "set_llm_api_key", None)
                if set_key is not None:
                    set_key(llm_api_key)
                    logger.info("Configured adapter with trial-specific LLM API key")

            runtime_env = resolve_litellm_runtime_env(config.litellm_mode or "external")
            runtime_errors = required_env_errors_for_mode(
                runtime_env, tracking_enabled=config.llm_tracking_enabled
            )
            if runtime_errors:
                raise RuntimeError(
                    "Invalid LiteLLM runtime configuration: "
                    + "; ".join(runtime_errors)
                )

            # Pass resolved runtime URL/key to oss-crs for all LiteLLM modes.
            # Prefer per-trial virtual key when tracking is enabled.
            litellm_runtime_key = (
                llm_api_key or runtime_env.api_key or runtime_env.master_key
            )
            litellm_runtime_url = runtime_env.direct_base_url
            if litellm_runtime_url and litellm_runtime_key:
                adapter.configure(
                    {
                        "litellm_runtime_url": litellm_runtime_url,
                        "litellm_runtime_api_key": litellm_runtime_key,
                    }
                )
                logger.info(
                    "Configured compose adapter for LiteLLM runtime "
                    f"(mode={config.litellm_mode or 'external'})"
                )
        else:
            logger.debug(
                "skip_litellm=true; skipping LiteLLM runtime and tracking setup"
            )

        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=snapshot_period,
            coverage_enabled=coverage_enabled,
            coverage_saturation_time=coverage_saturation_time,
            coverage_early_stop=coverage_early_stop,
            pov_early_stop=pov_early_stop,
            per_pov_verify_timeout=config.per_pov_verify_timeout,
            verify_timeout=config.verify_timeout,
            oss_fuzz_path=oss_fuzz_path,
            on_build_start=on_build_start,
            on_run_start=on_run_start,
            on_verification_start=on_verification_start,
            llm_tracker=llm_tracker,
            llm_api_key=llm_api_key,
            llm_trial_id=trial_id,
            llm_accounting_settle_seconds=config.llm_accounting_settle_seconds,
            max_pov_variants_per_cpv=effective_inputs.max_pov_variants_per_cpv,
            patch_verify_variants=effective_inputs.patch_verify_variants,
            pov_input_enabled=effective_inputs.pov_enabled,
            sarif_input_enabled=effective_inputs.hints_enabled,
            sarif_level=effective_inputs.hint_sarif_level,
            seed_corpus_enabled=effective_inputs.seed_corpus_enabled,
            seed_corpus_max_time=effective_inputs.seed_corpus_max_time,
            diff_input_enabled=effective_inputs.diff_enabled,
            redis_host=_resolve_redis_host(config),
            experiment_name=config.experiment,
            pov_dedup_strategy=config.pov_dedup_strategy,
            inc_image_policy=config.inc_image_policy,
            inc_image_registry=config.inc_image_registry,
            inc_image_max_pull_bytes=config.inc_image_max_pull_bytes,
            inc_image_pull_timeout=config.inc_image_pull_timeout_sec,
            local_image_prefix=config.project_image_prefix,
        )

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
            target_cpv_id=target_cpv_id,
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
                hints_enabled=effective_inputs.hints_enabled,
                hints_corpus_level=effective_inputs.hint_corpus_level,
                target_povs=effective_inputs.max_pov_variants_per_cpv,
                hint_sarif_level=effective_inputs.hint_sarif_level,
                seed_corpus_enabled=effective_inputs.seed_corpus_enabled,
                seed_corpus_max_time=effective_inputs.seed_corpus_max_time,
                pov_input_enabled=effective_inputs.pov_enabled,
                diff_enabled=effective_inputs.diff_enabled,
                patch_verify_variants=effective_inputs.patch_verify_variants,
                inputs=effective_inputs.to_dict(),
            ),
            worker_machine=runtime_worker_name,
            worker_trial_dir=str(trial_output_dir),
            build_mode=mode,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
            experiment_name=config.experiment,
            litellm_budget=config.litellm_cost_budget,
            cores_per_trial=config.resources.cores_per_trial
            if config.resources
            else None,
            memory_per_trial=config.resources.memory_per_trial
            if config.resources
            else None,
            experiment_config=config.model_dump(mode="json"),
        )
        metadata_file = trial_output_dir / "metadata.json"
        with metadata_file.open("w") as f:
            json.dump(file_metadata.model_dump(mode="json"), f, indent=2)
        logger.debug(f"Wrote trial metadata to {metadata_file}")

        # Compute memory bytes for cgroup enforcement
        memory_bytes = 0
        if allocated_memory:
            try:
                from crsbench.utils.size_parser import parse_size_to_bytes

                memory_bytes = parse_size_to_bytes(allocated_memory)
            except ValueError:
                logger.warning(f"Could not parse memory_per_trial: {allocated_memory}")

        # Run benchmark evaluation wrapped with cgroup lifecycle
        # Note: CRS is already configured via executor.configure_crs() above
        from crsbench.evaluation.resource_context import ResourceContext

        with ResourceContext(
            trial_name=trial_id,
            cpuset=allocated_cpus,
            memory_bytes=memory_bytes,
            use_cgroups=use_local_cgroup,
        ):
            try:
                result = runner.run_benchmark(
                    benchmark_harness=benchmark_harness,
                    mode=mode,  # Use mode from trial
                    crs_config={},  # Empty config - executor already configured
                    trial_output_dir=trial_output_dir,
                    oss_fuzz_path=oss_fuzz_path,
                    skip_verification=config.skip_verification,
                    sanitizer=sanitizer,
                    target_cpv_id=target_cpv_id,
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

        # Extract build/run timing from harness result (single harness per trial)
        build_time = None
        run_time = None
        if result.report.harness_results:
            harness_result = result.report.harness_results[0]
            build_time = harness_result.build_time
            run_time = harness_result.run_time

        # Create trial metadata
        metadata = TrialMetadata(
            experiment_filestore=str(config.experiment_filestore),
            max_total_time=config.max_total_time,
            timestamp_start=start_time,
            timestamp_end=time.time(),
            build_time=build_time,
            run_time=run_time,
            experiment_name=config.experiment,
            litellm_budget=config.litellm_cost_budget,
            cores_per_trial=config.resources.cores_per_trial
            if config.resources
            else None,
            memory_per_trial=config.resources.memory_per_trial
            if config.resources
            else None,
            target_cpv_id=target_cpv_id,
            worker_machine=runtime_worker_name,
            worker_trial_dir=str(trial_output_dir),
        )

        # Create trial result using Pydantic model
        trial_result = TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type,
            mode=mode,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
            success=result.success,
            execution_time=execution_time,
            povs_found=result.povs_found,
            total_povs=result.total_povs,
            patches_generated=result.report.patches_generated,
            patches_verified=result.report.patches_verified,
            patches_valid=result.report.patches_valid,
            report=result.report.to_dict(),
            metadata=metadata,
        )

        # Log completion message
        logger.info(trial_result.log_summary())

        def _write_final_metadata() -> None:
            if build_time is None and run_time is None:
                return
            file_metadata.build_time = build_time
            file_metadata.run_time = run_time
            with metadata_file.open("w") as f:
                json.dump(file_metadata.model_dump(mode="json"), f, indent=2)

        _publish_trial_terminal_artifacts(
            config=config,
            trial_output_dir=trial_output_dir,
            success=result.success,
            results_timestamp=results_timestamp,
            lifecycle_runtime=lifecycle_runtime,
            metadata_writer=_write_final_metadata,
        )

        _finish_job_lifecycle(
            lifecycle_runtime,
            success=result.success,
            detail=trial_result.error,
        )
        _stop_job_lifecycle_heartbeat(heartbeat_runtime)
        return trial_result

    except FileNotFoundError as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.error(f"[Trial {trial_num}] Benchmark not found: {error_msg}")
        _finish_job_lifecycle(
            lifecycle_runtime,
            success=False,
            detail=f"Benchmark not found: {e!s}",
        )
        _stop_job_lifecycle_heartbeat(heartbeat_runtime)
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            _publish_trial_terminal_artifacts(
                config=config,
                trial_output_dir=trial_output_dir,
                success=False,
                results_timestamp=results_timestamp,
                lifecycle_runtime=lifecycle_runtime,
            )
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type,
            mode=mode,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
            success=False,
            execution_time=execution_time,
            error=f"Benchmark not found: {e!s}",
            error_type="FileNotFoundError",
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
                target_cpv_id=target_cpv_id,
            ),
        )

    except BenchmarkFormatError as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.error(f"[Trial {trial_num}] Invalid benchmark format: {error_msg}")
        _finish_job_lifecycle(
            lifecycle_runtime,
            success=False,
            detail=f"Invalid benchmark format: {e!s}",
        )
        _stop_job_lifecycle_heartbeat(heartbeat_runtime)
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            _publish_trial_terminal_artifacts(
                config=config,
                trial_output_dir=trial_output_dir,
                success=False,
                results_timestamp=results_timestamp,
                lifecycle_runtime=lifecycle_runtime,
            )
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type,
            mode=mode,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
            success=False,
            execution_time=execution_time,
            error=f"Invalid benchmark format: {e!s}",
            error_type="BenchmarkFormatError",
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
                target_cpv_id=target_cpv_id,
            ),
        )

    except Exception as e:
        execution_time = time.time() - start_time
        error_msg = escape_loguru_braces(str(e))
        logger.exception("[Trial {}] Failed with error: {}", trial_num, error_msg)
        _finish_job_lifecycle(
            lifecycle_runtime,
            success=False,
            detail=str(e),
        )
        _stop_job_lifecycle_heartbeat(heartbeat_runtime)
        # Create .fail marker if trial_output_dir exists
        if "trial_output_dir" in locals() and trial_output_dir.exists():
            _publish_trial_terminal_artifacts(
                config=config,
                trial_output_dir=trial_output_dir,
                success=False,
                results_timestamp=results_timestamp,
                lifecycle_runtime=lifecycle_runtime,
            )
        return TrialResult(
            crs=crs,
            benchmark=benchmark,
            harness=harness_name,
            trial_num=trial_num,
            crs_type=crs_type,
            mode=mode,
            sanitizer=sanitizer,
            target_cpv_id=target_cpv_id,
            success=False,
            execution_time=execution_time,
            error=str(e),
            error_type=type(e).__name__,
            report={},
            metadata=TrialMetadata(
                timestamp_start=start_time,
                timestamp_end=time.time(),
                target_cpv_id=target_cpv_id,
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


def check_benchmarks_available(
    benchmarks: list[str], config: ExperimentConfig
) -> list[str]:
    """Check which benchmarks from config are missing on this machine.

    Returns list of missing benchmark names (empty = all present).
    """
    missing = []
    for name in benchmarks:
        try:
            _resolve_benchmark_path(name, config)
        except FileNotFoundError:
            missing.append(name)
    return missing


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
    error_msg += (
        "\n\nTo download missing benchmarks:\n"
        f"  crsbench download --dataset crsbench "
        f"--benchmarks {benchmark} --output-dir {default_benchmarks_root}"
    )

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
