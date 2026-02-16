"""Verify queue utilities for async POV verification.

This module provides functions for workers to enqueue POV verification
jobs and poll for results. The verify queue is consumed by evaluator
processes running on the same or different machines.

All verification uses per-POV granularity (verify_single_pov) for
fine-grained parallelism and individual retry.
"""

import time
from typing import Any, Optional

from crsbench.utils.logger import get_logger

try:
    import rq
    import rq.job

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = get_logger(__name__)

# Maximum POV size to enqueue (10MB). Larger POVs are skipped with a warning.
MAX_POV_SIZE_BYTES = 10 * 1024 * 1024


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

    queue_name = f"crsbench_{experiment_name}_verify"
    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Verify queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize verify queue: {e}")
        return None


def enqueue_single_pov(
    verify_queue: "rq.Queue",
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    pov_id: str,
    pov_data: bytes,
    job_timeout: int = 3600,
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
    )

    try:
        job = verify_queue.enqueue(
            "crsbench.distributed.evaluator_jobs.verify_single_pov",
            payload.to_dict(),
            job_timeout=job_timeout,
            result_ttl=-1,
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
                    # Return error as a verdict
                    exc_info = job.exc_info or "Unknown error"
                    completed.append(
                        {
                            "trial_id": "",
                            "benchmark": "",
                            "harness": "",
                            "verdict": {
                                "pov_id": "unknown",
                                "triggered_bug": False,
                                "status": "error",
                                "cpv_matches": [],
                                "error": str(exc_info)[:500],
                            },
                            "completed_at": time.time(),
                        }
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
