"""Verify queue utilities for async POV verification.

This module provides functions for workers to enqueue POV verification
jobs and poll for results. The verify queue is consumed by evaluator
processes running on the same or different machines.

All verification uses per-POV granularity (verify_single_pov) for
fine-grained parallelism and individual retry.
"""

import os
import time
from typing import Any, Optional

from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.utils.logger import get_logger

if REDIS_AVAILABLE:
    import rq
    import rq.job

logger = get_logger(__name__)

# Maximum POV size to enqueue (10MB). Larger POVs are skipped with a warning.
MAX_POV_SIZE_BYTES = 10 * 1024 * 1024


def _is_duplicate_job_enqueue_error(exc: Exception) -> bool:
    """Best-effort duplicate enqueue detection across RQ versions."""
    msg = str(exc).lower()
    return "already exists" in msg or "job id" in msg and "exists" in msg


def _enqueue_with_existing_reuse(
    queue: Any,
    job_func: str,
    payload: dict[str, Any],
    *,
    job_timeout: int,
    meta: dict[str, str],
    job_id: str,
    depends_on: Optional[list[Any]] = None,
) -> Any:
    """Enqueue an RQ job by deterministic ID and reuse existing duplicates."""
    try:
        return queue.enqueue(
            job_func,
            payload,
            job_timeout=job_timeout,
            result_ttl=-1,
            job_id=job_id,
            depends_on=depends_on,
            meta=meta,
        )
    except Exception as e:
        if not _is_duplicate_job_enqueue_error(e):
            raise
        existing = rq.job.Job.fetch(job_id, connection=queue.connection)
        logger.debug(f"Reusing existing POV RQ job {job_id}")
        return existing


def _error_result_from_rq_job(job: Any, *, default_error: str) -> dict[str, Any]:
    """Build a terminal error verdict preserving routing metadata."""
    raw_args = getattr(job, "args", None)
    raw_kwargs = getattr(job, "kwargs", None)
    payload: dict[str, Any] = {}
    if isinstance(raw_args, dict):
        payload = raw_args
    elif (
        isinstance(raw_args, (list, tuple))
        and raw_args
        and isinstance(raw_args[0], dict)
    ):
        payload = raw_args[0]
    elif isinstance(raw_kwargs, dict):
        payload = raw_kwargs
    pov_payload = payload.get("pov")
    pov_id = (
        pov_payload.get("pov_id", "unknown")
        if isinstance(pov_payload, dict)
        else "unknown"
    )
    return {
        "trial_id": payload.get("trial_id", ""),
        "benchmark": payload.get("benchmark", ""),
        "harness": payload.get("harness", ""),
        "verdict": {
            "pov_id": pov_id,
            "triggered_bug": False,
            "status": "error",
            "cpv_matches": [],
            "error": default_error[:500],
        },
        "completed_at": time.time(),
    }


def initialize_verify_queue(
    redis_host: str, experiment_name: str
) -> Optional["rq.Queue"]:
    """Initialize the verification queue for an experiment.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming

    Returns:
        RQ Queue instance, or None if Redis unavailable
    """
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed, verify queue unavailable")
        return None

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, _build_queue, queue_name = resolve_queue_names(experiment_name)
    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Verify queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize verify queue: {e}")
        return None


def initialize_build_queue(
    redis_host: str, experiment_name: str
) -> Optional["rq.Queue"]:
    """Initialize the build queue used by async POV verification."""
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed, build queue unavailable")
        return None

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, queue_name, _verify_queue = resolve_queue_names(experiment_name)
    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Build queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize build queue: {e}")
        return None


def build_variant_rq_job_id(
    *,
    benchmark: str,
    variant_name: str,
    source_mode: str,
    use_inc_build: bool,
) -> str:
    """Build a deterministic RQ job ID for async POV build prerequisites."""
    mode = "inc" if use_inc_build else "clean"
    return f"build-single/{benchmark}/{variant_name}/{source_mode}/{mode}"


def enqueue_ci_job(
    queue: Any,
    experiment_name: str,
    job: Any,
    *,
    job_timeout: int = 3600,
    cpu_tag: Optional[str] = None,
    depends_on: Optional[list[Any]] = None,
    job_id: Optional[str] = None,
) -> Any:
    """Enqueue a serialized CI job with deterministic reuse semantics."""
    from crsbench.distributed.ci_jobs import serialize_ci_job

    effective_cpu_tag = cpu_tag or os.environ.get("CRSBENCH_JOB_CPU_TAG")
    job_meta = {"experiment_name": experiment_name}
    if effective_cpu_tag:
        job_meta["cpu_tag"] = effective_cpu_tag

    resolved_job_id = job_id or getattr(job, "job_id", "")
    if not resolved_job_id:
        raise ValueError("enqueue_ci_job requires a deterministic job_id")

    return _enqueue_with_existing_reuse(
        queue,
        "crsbench.distributed.ci_jobs.execute_ci_job",
        serialize_ci_job(job),
        job_timeout=job_timeout,
        meta=job_meta,
        job_id=resolved_job_id,
        depends_on=depends_on,
    )


def enqueue_single_pov(
    verify_queue: "rq.Queue",
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    pov_id: str,
    pov_data: bytes,
    *,
    sanitizer: Optional[str] = None,
    job_timeout: int = 3600,
    cpu_tag: Optional[str] = None,
    build_job_ids: Optional[list[str]] = None,
    depends_on: Optional[list[Any]] = None,
    source_mode: str = "pkgs",
    use_inc_build: bool = True,
) -> Optional[str]:
    """Enqueue a single POV for async verification.

    Used by POVVerificationManager in async mode to enqueue individual
    POVs as they are discovered during CRS execution.

    Args:
        verify_queue: RQ verify queue instance
        experiment_name: Experiment identifier
        trial_id: Trial identifier for result correlation
        benchmark: Benchmark name
        harness: Harness name
        pov_id: POV identifier (filename)
        pov_data: Raw POV file content
        sanitizer: Sanitizer scope for selecting verification builds
        job_timeout: Job execution timeout in seconds

    Returns:
        Job ID if enqueued, None on error
    """
    from crsbench.distributed.evaluator_jobs import (
        EmbeddedPov,
        SinglePovPayload,
    )

    if len(pov_data) > MAX_POV_SIZE_BYTES:
        logger.warning(
            f"Skipping POV {pov_id}: {len(pov_data)} bytes exceeds "
            f"{MAX_POV_SIZE_BYTES} byte limit"
        )
        return None

    embedded_pov = EmbeddedPov.from_bytes(pov_id, pov_data)
    payload = SinglePovPayload(
        experiment_name=experiment_name,
        trial_id=trial_id,
        benchmark=benchmark,
        harness=harness,
        pov=embedded_pov,
        enqueued_at=time.time(),
        sanitizer=sanitizer,
        build_job_ids=list(build_job_ids or []),
        source_mode=source_mode,
        use_inc_build=use_inc_build,
    )

    try:
        effective_cpu_tag = cpu_tag or os.environ.get("CRSBENCH_JOB_CPU_TAG")
        job_meta = {"experiment_name": experiment_name}
        if effective_cpu_tag:
            job_meta["cpu_tag"] = effective_cpu_tag

        job = verify_queue.enqueue(
            "crsbench.distributed.evaluator_jobs.verify_single_pov",
            payload.to_dict(),
            job_timeout=job_timeout,
            result_ttl=-1,
            depends_on=depends_on,
            meta=job_meta,
        )
        logger.debug(
            f"Enqueued single POV verify job {job.id[:8]} "
            f"for {benchmark}/{harness} pov={pov_id}"
        )
        return job.id
    except Exception as e:
        logger.warning(f"Failed to enqueue single POV verify job: {e}")
        return None


def poll_single_pov_verdicts(
    redis_host: str,
    job_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Poll for completed single-POV verification verdicts.

    Non-blocking: returns immediately with completed results and
    remaining (still-pending) job IDs.

    Args:
        redis_host: Redis server hostname
        job_ids: List of verify job IDs to check

    Returns:
        Tuple of (completed_results, remaining_job_ids)
    """
    if not REDIS_AVAILABLE or not job_ids:
        return [], list(job_ids)

    completed: list[dict[str, Any]] = []
    remaining: list[str] = []

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        for job_id in job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=redis_conn)
                status = job.get_status()
                if status == "finished" and job.result is not None:
                    completed.append(job.result)
                elif status == "failed":
                    exc_info = job.exc_info or "Unknown error"
                    completed.append(
                        _error_result_from_rq_job(job, default_error=str(exc_info))
                    )
                else:
                    remaining.append(job_id)
            except Exception as e:
                logger.debug(f"Could not fetch verify job {job_id[:8]}: {e}")
                remaining.append(job_id)

    except Exception as e:
        logger.warning(f"Failed to poll single POV verdicts: {e}")
        remaining.extend(job_ids)

    return completed, remaining
