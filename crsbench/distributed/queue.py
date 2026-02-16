"""Queue utilities for Redis-backed job queue management.

This module provides utilities for initializing and managing Redis-backed RQ queues
for distributed CRS trial execution.
"""

import os
from typing import List, Optional

from crsbench.utils.logger import get_logger

try:
    import redis
    import rq
    import rq.job

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None  # type: ignore[assignment]
    rq = None  # type: ignore[assignment]

logger = get_logger(__name__)


def create_redis_connection(
    redis_host: str,
    *,
    socket_connect_timeout: int = 5,
) -> "redis.Redis":
    """Create a Redis connection with auto-detection of password requirement.

    Reads REDIS_PASSWORD from environment. If set but the server doesn't
    require auth (stale .env), retries without password. If not set but
    the server requires auth, raises a clear error.

    Args:
        redis_host: Redis server hostname or IP address
        socket_connect_timeout: Connection timeout in seconds

    Returns:
        Connected and pinged Redis client

    Raises:
        redis.AuthenticationError: If server requires a password but
            REDIS_PASSWORD is not set
        redis.ConnectionError: If server is unreachable
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed execution. "
            "Install with: pip install redis rq"
        )

    password = os.environ.get("REDIS_PASSWORD") or None

    if password:
        # Try with password first
        try:
            conn = redis.Redis(
                host=redis_host,
                password=password,
                socket_connect_timeout=socket_connect_timeout,
            )
            conn.ping()
            return conn
        except redis.AuthenticationError:
            # Server doesn't need auth — stale REDIS_PASSWORD in env
            logger.info(
                "REDIS_PASSWORD is set but server requires no auth, "
                "connecting without password"
            )
            conn = redis.Redis(
                host=redis_host,
                socket_connect_timeout=socket_connect_timeout,
            )
            conn.ping()
            return conn

    # No password in env — connect without
    try:
        conn = redis.Redis(
            host=redis_host,
            socket_connect_timeout=socket_connect_timeout,
        )
        conn.ping()
        return conn
    except redis.AuthenticationError:
        raise redis.AuthenticationError(
            "Redis server requires authentication. Set REDIS_PASSWORD environment variable."
        ) from None


def check_redis_available(redis_host: str, timeout: int = 2) -> bool:
    """
    Check if Redis server is reachable.

    Args:
        redis_host: Redis server hostname or IP address
        timeout: Connection timeout in seconds

    Returns:
        bool: True if Redis is available and responding to ping

    Example:
        >>> if check_redis_available('localhost'):
        ...     print("Redis is available")
    """
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed")
        return False

    try:
        create_redis_connection(redis_host, socket_connect_timeout=timeout)
        logger.debug(f"Redis server at {redis_host} is reachable")
        return True
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.debug(f"Redis server at {redis_host} is not reachable: {e}")
        return False
    except Exception as e:
        logger.warning(f"Unexpected error checking Redis availability: {e}")
        return False


def initialize_queue(redis_host: str, experiment_name: str) -> Optional["rq.Queue"]:
    """
    Initialize Redis-backed RQ queue for an experiment.

    Args:
        redis_host: Redis server hostname or IP address
        experiment_name: Experiment identifier for queue naming

    Returns:
        rq.Queue: Initialized RQ queue, or None if Redis unavailable

    Raises:
        RuntimeError: If Redis/RQ packages not installed
        redis.ConnectionError: If cannot connect to Redis server

    Example:
        >>> queue = initialize_queue('localhost', 'my-experiment')
        >>> if queue:
        ...     print(f"Queue name: {queue.name}")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed execution. "
            "Install with: pip install redis rq"
        )

    queue_name = f"crsbench_{experiment_name}"
    logger.info(f"Initializing queue: {queue_name}")

    try:
        redis_connection = create_redis_connection(redis_host)

        queue = rq.Queue(queue_name, connection=redis_connection)  # type: ignore[attr-defined]
        logger.info(f"Queue initialized successfully: {queue_name}")
        return queue

    except redis.ConnectionError as e:
        logger.error(f"Failed to connect to Redis at {redis_host}: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to initialize queue: {e}")
        raise


def get_all_jobs(queue: "rq.Queue") -> List["rq.job.Job"]:
    """
    Get all jobs currently in the queue.

    Args:
        queue: RQ queue instance

    Returns:
        List[rq.job.Job]: List of Job objects in the queue

    Example:
        >>> queue = initialize_queue('localhost', 'test-exp')
        >>> jobs = get_all_jobs(queue)
        >>> for job in jobs:
        ...     print(f"Job {job.id}: {job.get_status()}")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    try:
        job_ids = queue.get_job_ids()
        jobs = rq.job.Job.fetch_many(job_ids, queue.connection)  # type: ignore[attr-defined]
        return [job for job in jobs if job is not None]
    except Exception as e:
        logger.error(f"Failed to fetch jobs from queue: {e}")
        return []


# TODO: make a pydantic mode for queue stats
def get_queue_stats(queue: "rq.Queue") -> dict:
    """
    Get statistics about the current queue state.

    Args:
        queue: RQ queue instance

    Returns:
        dict: Dictionary containing queue statistics

    Example:
        >>> queue = initialize_queue('localhost', 'test-exp')
        >>> stats = get_queue_stats(queue)
        >>> print(f"Queued: {stats['queued']}, Finished: {stats['finished']}")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    try:
        # Get worker count
        workers = rq.Worker.all(connection=queue.connection)  # type: ignore[attr-defined]
        worker_count = len(workers)

        return {
            "queued": queue.count,
            "started": queue.started_job_registry.count,
            "deferred": queue.deferred_job_registry.count,
            "finished": queue.finished_job_registry.count,
            "failed": queue.failed_job_registry.count,
            "scheduled": queue.scheduled_job_registry.count
            if hasattr(queue, "scheduled_job_registry")
            else 0,
            "workers": worker_count,
        }
    except Exception as e:
        logger.error(f"Failed to get queue stats: {e}")
        return {
            "queued": 0,
            "started": 0,
            "deferred": 0,
            "finished": 0,
            "failed": 0,
            "scheduled": 0,
        }


def clear_queue(queue: "rq.Queue") -> int:
    """
    Clear all jobs from the queue.

    Args:
        queue: RQ queue instance

    Returns:
        int: Number of jobs removed

    Warning:
        This will remove all pending jobs. Use with caution.

    Example:
        >>> queue = initialize_queue('localhost', 'test-exp')
        >>> removed = clear_queue(queue)
        >>> print(f"Removed {removed} jobs")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    try:
        job_ids = queue.get_job_ids()
        count = len(job_ids)

        for job_id in job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                job.delete()
            except Exception as e:
                logger.warning(f"Failed to delete job {job_id}: {e}")

        logger.info(f"Cleared {count} jobs from queue {queue.name}")
        return count

    except Exception as e:
        logger.error(f"Failed to clear queue: {e}")
        return 0


def get_trial_key(job: "rq.job.Job") -> str:
    """
    Generate unique trial key from job metadata.

    Args:
        job: RQ job instance

    Returns:
        str: Unique trial key in format "crs:benchmark:harness:mode:trial_num"

    Example:
        >>> key = get_trial_key(job)
        >>> print(key)  # "atlantis-c:libxml2:fuzz_test:delta:1"
    """
    meta = job.meta
    return f"{meta['crs']}:{meta['benchmark']}:{meta['harness']}:{meta['mode']}:{meta['trial_num']}"


def get_existing_trials(queue: "rq.Queue") -> dict[str, dict[str, "rq.job.Job"]]:
    """
    Get all existing jobs grouped by status with trial keys.

    Args:
        queue: RQ queue instance

    Returns:
        dict: Jobs grouped by status:
            {
                "queued": {trial_key: job, ...},
                "started": {trial_key: job, ...},
                "finished": {trial_key: job, ...},
                "failed": {trial_key: job, ...},
            }

    Example:
        >>> queue = initialize_queue('localhost', 'test-exp')
        >>> existing = get_existing_trials(queue)
        >>> print(f"Queued: {len(existing['queued'])}")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    result = {
        "queued": {},
        "started": {},
        "finished": {},
        "failed": {},
    }

    try:
        # Get queued jobs
        for job in get_all_jobs(queue):
            if job.is_queued:
                result["queued"][get_trial_key(job)] = job

        # Get started jobs
        for job_id in queue.started_job_registry.get_job_ids():
            try:
                job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                if job:
                    result["started"][get_trial_key(job)] = job
            except Exception as e:
                logger.warning(f"Failed to fetch started job {job_id}: {e}")

        # Get finished jobs
        for job_id in queue.finished_job_registry.get_job_ids():
            try:
                job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                if job:
                    result["finished"][get_trial_key(job)] = job
            except Exception as e:
                logger.warning(f"Failed to fetch finished job {job_id}: {e}")

        # Get failed jobs
        for job_id in queue.failed_job_registry.get_job_ids():
            try:
                job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                if job:
                    result["failed"][get_trial_key(job)] = job
            except Exception as e:
                logger.warning(f"Failed to fetch failed job {job_id}: {e}")

        return result

    except Exception as e:
        logger.error(f"Failed to get existing trials: {e}")
        return result


def requeue_failed_jobs(queue: "rq.Queue", failed_jobs: list["rq.job.Job"]) -> int:
    """
    Requeue failed jobs for retry (at end of queue - FIFO).

    Args:
        queue: RQ queue instance
        failed_jobs: List of failed Job instances

    Returns:
        int: Number of jobs requeued

    Example:
        >>> existing = get_existing_trials(queue)
        >>> count = requeue_failed_jobs(queue, list(existing['failed'].values()))
        >>> print(f"Requeued {count} failed jobs")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    count = 0
    for job in failed_jobs:
        try:
            queue.enqueue_job(job)
            count += 1
            logger.debug(f"Requeued failed job {job.id[:8]}")
        except Exception as e:
            logger.warning(f"Failed to requeue job {job.id[:8]}: {e}")

    if count > 0:
        logger.info(f"Requeued {count} failed jobs")
    return count


def handle_orphaned_jobs(
    queue: "rq.Queue", started_jobs: dict[str, "rq.job.Job"]
) -> int:
    """
    Move orphaned started jobs to failed and requeue for retry.

    Orphaned jobs are started jobs with no workers connected (workers died mid-execution).

    Args:
        queue: RQ queue instance
        started_jobs: Dict of trial_key -> Job for started jobs

    Returns:
        int: Number of orphaned jobs handled

    Example:
        >>> existing = get_existing_trials(queue)
        >>> count = handle_orphaned_jobs(queue, existing['started'])
        >>> print(f"Handled {count} orphaned jobs")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    # Check if any workers are connected
    stats = get_queue_stats(queue)
    if stats.get("workers", 0) > 0:
        logger.debug("Workers exist - not handling started jobs as orphaned")
        return 0

    count = 0
    for job in started_jobs.values():
        try:
            # Move to failed registry
            job.set_status(rq.job.JobStatus.FAILED)  # type: ignore[attr-defined]
            # Requeue for retry
            queue.enqueue_job(job)
            count += 1
            logger.debug(f"Handled orphaned job {job.id[:8]}")
        except Exception as e:
            logger.warning(f"Failed to handle orphaned job {job.id[:8]}: {e}")

    if count > 0:
        logger.info(f"Handled {count} orphaned jobs (moved to failed + requeued)")
    return count
