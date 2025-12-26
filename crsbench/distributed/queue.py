"""Queue utilities for Redis-backed job queue management.

This module provides utilities for initializing and managing Redis-backed RQ queues
for distributed CRS trial execution.
"""

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
        client = redis.Redis(host=redis_host, socket_connect_timeout=timeout)
        client.ping()
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
        redis_connection = redis.Redis(host=redis_host)
        # Test connection
        redis_connection.ping()

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
        return {
            "queued": queue.count,
            "started": queue.started_job_registry.count,
            "deferred": queue.deferred_job_registry.count,
            "finished": queue.finished_job_registry.count,
            "failed": queue.failed_job_registry.count,
            "scheduled": queue.scheduled_job_registry.count
            if hasattr(queue, "scheduled_job_registry")
            else 0,
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
