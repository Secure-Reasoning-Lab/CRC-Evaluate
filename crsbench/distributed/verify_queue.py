"""Verify queue utilities for async POV verification.

This module provides functions for workers to enqueue POV verification
jobs and poll for results. The verify queue is consumed by evaluator
processes running on the same or different machines.
"""

import os
import time
from pathlib import Path
from typing import Any, Optional

from crsbench.utils.logger import get_logger

try:
    import redis
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
        redis_password = os.environ.get("REDIS_PASSWORD") or None
        redis_conn = redis.Redis(
            host=redis_host,
            password=redis_password,
            socket_connect_timeout=5,
        )
        redis_conn.ping()
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Verify queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize verify queue: {e}")
        return None


def enqueue_verify_job(
    verify_queue: "rq.Queue",
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    pov_dir: Path,
    job_timeout: int = 3600,
) -> Optional[str]:
    """Enqueue POVs from a directory for async verification.

    Reads all POV files from the directory, embeds their content in the
    job payload, and enqueues a single verification job.

    Args:
        verify_queue: RQ verify queue instance
        experiment_name: Experiment identifier
        trial_id: Trial identifier for result correlation
        benchmark: Benchmark name
        harness: Harness name
        pov_dir: Directory containing POV files
        job_timeout: Job execution timeout in seconds

    Returns:
        Job ID if enqueued, None if no POVs or error
    """
    from crsbench.distributed.evaluator_jobs import (
        EmbeddedPov,
        VerificationJobPayload,
    )

    if not pov_dir.exists():
        logger.debug(f"No POV directory found: {pov_dir}")
        return None

    # Collect POV files
    pov_files = sorted(pov_dir.iterdir()) if pov_dir.is_dir() else []
    pov_files = [f for f in pov_files if f.is_file()]

    if not pov_files:
        logger.debug(f"No POV files in {pov_dir}")
        return None

    # Read and embed POV data
    embedded_povs = []
    for pov_file in pov_files:
        file_size = pov_file.stat().st_size
        if file_size > MAX_POV_SIZE_BYTES:
            logger.warning(
                f"Skipping POV {pov_file.name}: {file_size} bytes exceeds "
                f"{MAX_POV_SIZE_BYTES} byte limit"
            )
            continue

        pov_data = pov_file.read_bytes()
        embedded_povs.append(EmbeddedPov.from_bytes(pov_file.name, pov_data))

    if not embedded_povs:
        logger.debug("No valid POVs to enqueue after size filtering")
        return None

    # Create payload
    payload = VerificationJobPayload(
        experiment_name=experiment_name,
        trial_id=trial_id,
        benchmark=benchmark,
        harness=harness,
        povs=embedded_povs,
        enqueued_at=time.time(),
    )

    # Enqueue job (fire-and-forget)
    try:
        job = verify_queue.enqueue(
            "crsbench.distributed.evaluator_jobs.verify_povs",
            payload.to_dict(),
            job_timeout=job_timeout,
            result_ttl=-1,
        )
        logger.info(
            f"Enqueued verify job {job.id[:8]} with {len(embedded_povs)} POVs "
            f"for {benchmark}/{harness} (trial {trial_id})"
        )
        return job.id
    except Exception as e:
        logger.warning(f"Failed to enqueue verify job: {e}")
        return None


def poll_verify_results(
    redis_host: str,
    verify_job_ids: list[str],
) -> list[dict[str, Any]]:
    """Poll for completed verification results.

    Checks each verify job ID and returns results for completed jobs.
    Non-blocking: returns immediately with whatever results are available.

    Args:
        redis_host: Redis server hostname
        verify_job_ids: List of verify job IDs to check

    Returns:
        List of VerificationResult dicts for completed jobs
    """
    if not REDIS_AVAILABLE or not verify_job_ids:
        return []

    results = []
    try:
        redis_password = os.environ.get("REDIS_PASSWORD") or None
        redis_conn = redis.Redis(host=redis_host, password=redis_password)

        for job_id in verify_job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=redis_conn)
                if job.result is not None:
                    results.append(job.result)
            except Exception as e:
                logger.debug(f"Could not fetch verify job {job_id[:8]}: {e}")

    except Exception as e:
        logger.warning(f"Failed to poll verify results: {e}")

    return results


def enqueue_trial_povs(
    redis_host: str,
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    trial_output_dir: Path,
    job_timeout: int = 3600,
) -> list[str]:
    """Convenience function to enqueue all POVs from a completed trial.

    Used by workers after a trial completes to enqueue POVs for async
    verification. This is the fire-and-forget entry point.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        trial_id: Trial identifier
        benchmark: Benchmark name
        harness: Harness name
        trial_output_dir: Trial output directory
        job_timeout: Job execution timeout

    Returns:
        List of enqueued verify job IDs (may be empty)
    """
    verify_queue = initialize_verify_queue(redis_host, experiment_name)
    if verify_queue is None:
        return []

    pov_dir = trial_output_dir / "output" / "povs"
    job_id = enqueue_verify_job(
        verify_queue=verify_queue,
        experiment_name=experiment_name,
        trial_id=trial_id,
        benchmark=benchmark,
        harness=harness,
        pov_dir=pov_dir,
        job_timeout=job_timeout,
    )

    return [job_id] if job_id else []
