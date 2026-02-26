"""Patch verification queue utilities for distributed patch build+verify.

This module provides functions for enqueuing patch build and verify jobs
to the evaluator's Redis queues and polling for results. Patch builds go
to the BUILD queue (multi-CPU allocation) and patch verify jobs (POV test,
unit test) go to the VERIFY queue (1 CPU) with RQ dependency on the build.

Follows the same pattern as verify_queue.py for POV verification.
"""

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.utils.logger import get_logger

if REDIS_AVAILABLE:
    import rq
    import rq.job

if TYPE_CHECKING:
    import redis

logger = get_logger(__name__)


def _error_result_from_rq_job(job: Any, *, default_error: str) -> dict[str, Any]:
    """Build a terminal error verdict from an RQ patch verify job."""
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
    patch_payload = payload.get("patch")
    patch_id = (
        patch_payload.get("patch_id", "") if isinstance(patch_payload, dict) else ""
    )
    return {
        "trial_id": payload.get("trial_id", ""),
        "benchmark": payload.get("benchmark", ""),
        "harness": payload.get("harness", ""),
        "cpv_id": payload.get("cpv_id", ""),
        "patch_id": patch_id,
        "pov_test_passed": None,
        "unit_test_passed": None,
        "status": "error",
        "details": "",
        "error": default_error[:500],
        "completed_at": time.time(),
    }


def initialize_patch_queues(
    redis_host: str, experiment_name: str
) -> tuple[Optional["rq.Queue"], Optional["rq.Queue"]]:
    """Initialize build and verify queues for patch verification.

    Patch jobs reuse the same queues as POV verification and regular CI:
    build queue for patch builds (multi-CPU), verify queue for patch tests
    (1 CPU). The evaluator's ci_supervisor handles both transparently.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming

    Returns:
        Tuple of (build_queue, verify_queue), or (None, None) if
        Redis unavailable
    """
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed, patch queues unavailable")
        return None, None

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, build_queue_name, verify_queue_name = resolve_queue_names(
        experiment_name
    )

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        build_queue = rq.Queue(build_queue_name, connection=redis_conn)
        verify_queue = rq.Queue(verify_queue_name, connection=redis_conn)

        logger.info(
            f"Patch queues initialized: build={build_queue_name}, "
            f"verify={verify_queue_name}"
        )
        return build_queue, verify_queue

    except Exception as e:
        logger.warning(f"Failed to initialize patch queues: {e}")
        return None, None


def enqueue_patch_jobs(
    build_queue: "rq.Queue",
    verify_queue: "rq.Queue",
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    patches: list[tuple[str, str, Path]],
    sanitizer: str = "address",
    source_mode: str = "pkgs",
    *,
    verify_variants: bool = False,
    test_mode: str = "FULL",
    use_inc_build: bool = False,
    job_timeout: int = 3600,
    cpu_tag: Optional[str] = None,
) -> list[str]:
    """Enqueue patch build and verify jobs to evaluator queues.

    For each patch, enqueues a build job to the build queue and a verify
    job to the verify queue with RQ dependency on the build job. The
    verify job waits for the build to complete before starting.

    Args:
        build_queue: RQ build queue instance
        verify_queue: RQ verify queue instance
        experiment_name: Experiment identifier
        trial_id: Trial identifier for result correlation
        benchmark: Benchmark name
        harness: Harness name
        patches: List of (cpv_id, patch_id, patch_path) tuples
        sanitizer: Sanitizer to use for builds
        source_mode: Source mode for builds
        verify_variants: Whether to verify against all POV variants per CPV
        test_mode: Patch unit-test mode (FULL or RTS)
        use_inc_build: Whether to use incremental build
        job_timeout: Job execution timeout in seconds

    Returns:
        List of verify job IDs (for polling final results)
    """
    from crsbench.distributed.patch_evaluator_jobs import (
        EmbeddedPatch,
        PatchJobPayload,
    )

    verify_job_ids: list[str] = []

    for cpv_id, patch_id, patch_path in patches:
        try:
            embedded_patch = EmbeddedPatch.from_file(patch_id, cpv_id, patch_path)
            payload = PatchJobPayload(
                experiment_name=experiment_name,
                trial_id=trial_id,
                benchmark=benchmark,
                harness=harness,
                cpv_id=cpv_id,
                patch=embedded_patch,
                sanitizer=sanitizer,
                source_mode=source_mode,
                verify_variants=verify_variants,
                test_mode=test_mode,
                use_inc_build=use_inc_build,
                enqueued_at=time.time(),
            )

            payload_dict = payload.to_dict()

            # Enqueue build job to build queue (multi-CPU)
            effective_cpu_tag = cpu_tag or os.environ.get("CRSBENCH_JOB_CPU_TAG")
            job_meta = {"cpu_tag": effective_cpu_tag} if effective_cpu_tag else {}

            build_rq_job = build_queue.enqueue(
                "crsbench.distributed.patch_evaluator_jobs.execute_patch_build",
                payload_dict,
                job_timeout=job_timeout,
                result_ttl=-1,
                meta=job_meta,
            )
            logger.debug(
                f"Enqueued patch build job {build_rq_job.id[:8]} "
                f"for {benchmark}/{cpv_id} patch={patch_id}"
            )

            # Enqueue verify job to verify queue (1 CPU), depends on build
            verify_rq_job = verify_queue.enqueue(
                "crsbench.distributed.patch_evaluator_jobs.execute_patch_verify",
                payload_dict,
                job_timeout=job_timeout,
                result_ttl=-1,
                depends_on=[build_rq_job],
                meta=job_meta,
            )
            logger.debug(
                f"Enqueued patch verify job {verify_rq_job.id[:8]} "
                f"for {benchmark}/{cpv_id} patch={patch_id} "
                f"(depends on build {build_rq_job.id[:8]})"
            )

            verify_job_ids.append(verify_rq_job.id)

        except Exception as e:
            logger.warning(
                f"Failed to enqueue patch jobs for {benchmark}/{cpv_id} "
                f"patch={patch_id}: {e}"
            )

    logger.info(
        f"Enqueued {len(verify_job_ids)}/{len(patches)} patch job pairs "
        f"for {benchmark} trial={trial_id}"
    )
    return verify_job_ids


def poll_patch_verdicts(
    redis_host: str,
    job_ids: list[str],
    redis_conn: Optional["redis.Redis"] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Poll for completed patch verification verdicts.

    Non-blocking: returns immediately with completed results and
    remaining (still-pending) job IDs.

    Args:
        redis_host: Redis server hostname
        job_ids: List of verify job IDs to check
        redis_conn: Existing Redis connection (avoids reconnect per call)

    Returns:
        Tuple of (completed_results, remaining_job_ids)
    """
    if not REDIS_AVAILABLE or not job_ids:
        return [], list(job_ids)

    completed: list[dict[str, Any]] = []
    remaining: list[str] = []

    try:
        if redis_conn is None:
            from crsbench.distributed.queue import create_redis_connection

            redis_conn = create_redis_connection(redis_host)

        for job_id in job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=redis_conn)
                status = job.get_status()
                if status == "finished":
                    if isinstance(job.result, dict):
                        completed.append(job.result)
                    elif job.result is not None:
                        completed.append(
                            _error_result_from_rq_job(
                                job,
                                default_error=(
                                    "Patch verification finished with invalid "
                                    f"result payload type: {type(job.result).__name__}"
                                ),
                            )
                        )
                    else:
                        completed.append(
                            _error_result_from_rq_job(
                                job,
                                default_error="Patch verification finished without a result payload",
                            )
                        )
                elif status == "failed":
                    completed.append(
                        _error_result_from_rq_job(
                            job, default_error=str(job.exc_info or "Unknown error")
                        )
                    )
                elif status in {"stopped", "canceled", "cancelled"}:
                    completed.append(
                        _error_result_from_rq_job(
                            job,
                            default_error=(
                                "Patch verification terminated with non-success "
                                f"job status: {status}"
                            ),
                        )
                    )
                else:
                    remaining.append(job_id)
            except Exception as e:
                logger.debug(f"Could not fetch patch verify job {job_id[:8]}: {e}")
                remaining.append(job_id)

    except Exception as e:
        logger.warning(f"Failed to poll patch verdicts: {e}")
        remaining.extend(job_ids)

    return completed, remaining


def drain_patch_verdicts(
    redis_host: str,
    job_ids: list[str],
    poll_interval: float = 5.0,
    timeout: float = 7200.0,
) -> list[dict[str, Any]]:
    """Blocking poll: wait until all patch jobs complete or timeout.

    Polls for completed patch verdicts in a loop, recovering orphaned
    deferred jobs (RQ race condition) and logging progress periodically.

    Args:
        redis_host: Redis server hostname
        job_ids: List of verify job IDs to drain
        poll_interval: Seconds between poll attempts
        timeout: Maximum wait time in seconds

    Returns:
        List of all completed result dicts (may be partial on timeout)
    """
    from crsbench.distributed.queue import create_redis_connection

    redis_conn = create_redis_connection(redis_host)

    all_completed: list[dict[str, Any]] = []
    remaining = list(job_ids)
    total = len(job_ids)
    start_time = time.monotonic()
    last_recovery = start_time

    # Recovery interval for orphaned deferred jobs
    recovery_interval = 30.0

    while remaining:
        elapsed = time.monotonic() - start_time
        if elapsed >= timeout:
            logger.warning(
                f"Patch verdict drain timed out after {elapsed:.0f}s. "
                f"Completed {len(all_completed)}/{total}, "
                f"remaining {len(remaining)}"
            )
            break

        completed, remaining = poll_patch_verdicts(
            redis_host, remaining, redis_conn=redis_conn
        )
        all_completed.extend(completed)

        # Periodically recover orphaned deferred jobs
        now = time.monotonic()
        if remaining and (now - last_recovery) >= recovery_interval:
            _try_recover_deferred_jobs(redis_host, remaining, redis_conn=redis_conn)
            last_recovery = now

        if remaining:
            logger.info(
                f"Waiting for {len(remaining)}/{total} patch jobs... "
                f"({len(all_completed)} completed)"
            )
            time.sleep(poll_interval)

    if not remaining:
        logger.info(f"All {total} patch jobs completed")

    return all_completed


def _try_recover_deferred_jobs(
    redis_host: str,
    job_ids: list[str],
    redis_conn: Optional["redis.Redis"] = None,
) -> None:
    """Attempt to recover orphaned deferred patch verify jobs.

    Uses _recover_orphaned_deferred_jobs from ci_jobs.py to handle the
    RQ 2.x race condition where finished build jobs fail to move their
    dependent verify jobs from deferred to queued state.

    Args:
        redis_host: Redis server hostname
        job_ids: Job IDs that are still pending
        redis_conn: Existing Redis connection (avoids reconnect per call)
    """
    try:
        if redis_conn is None:
            from crsbench.distributed.queue import create_redis_connection

            redis_conn = create_redis_connection(redis_host)

        # Fetch jobs and find which queues they belong to
        rq_jobs: dict[str, rq.job.Job] = {}
        pending: set[str] = set()
        queues: set[str] = set()

        for job_id in job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=redis_conn)
                rq_jobs[job_id] = job
                pending.add(job_id)
                if job.origin:
                    queues.add(job.origin)
            except Exception:
                pass

        if not rq_jobs:
            return

        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs

        for queue_name in queues:
            queue = rq.Queue(queue_name, connection=redis_conn)
            recovered = _recover_orphaned_deferred_jobs(queue, rq_jobs, pending)
            if recovered:
                logger.info(
                    f"Recovered {recovered} orphaned deferred patch jobs "
                    f"from queue {queue_name}"
                )

    except Exception as e:
        logger.debug(f"Deferred job recovery failed: {e}")
