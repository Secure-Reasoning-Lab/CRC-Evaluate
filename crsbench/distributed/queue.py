"""Queue utilities for Redis-backed job queue management.

This module provides utilities for initializing and managing Redis-backed RQ queues
for distributed CRS trial execution.
"""

import os
import re
import time
from datetime import datetime, timezone
from enum import StrEnum
from typing import List, Literal, Optional

from crsbench.distributed.job_lifecycle import JobLifecycleStore, JobState
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

# Regex for valid queue name components (alphanumeric, underscores, hyphens)
_VALID_QUEUE_COMPONENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

QUEUE_MODEL_ENV = "CRSBENCH_QUEUE_MODEL"
QUEUE_MODEL_FLAT = "flat"
QUEUE_MODEL_PER_EXPERIMENT = "per-experiment"

FLAT_TRIAL_QUEUE = "crsbench_trial"
FLAT_BUILD_QUEUE = "crsbench_build"
FLAT_VERIFY_QUEUE = "crsbench_verify"


def build_trial_key(
    *,
    crs: str,
    benchmark: str,
    harness: str,
    mode: str | object,
    sanitizer: str | object,
    trial_num: int | object,
    target_cpv_id: str | None,
) -> str:
    """Build the canonical distributed trial key used across queue state."""
    return (
        f"{crs}:{benchmark}:{harness}:"
        f"{mode if mode is not None else '-'}:"
        f"{sanitizer if sanitizer is not None else '-'}:"
        f"{trial_num if trial_num is not None else '-'}:"
        f"{target_cpv_id or '-'}"
    )


def get_job_experiment_name(job: "rq.job.Job") -> str | None:
    """Extract experiment name from an RQ job (supports old/new payload layouts)."""
    meta = job.meta or {}
    experiment = meta.get("experiment_name") or meta.get("experiment")
    if isinstance(experiment, str) and experiment.strip():
        return experiment.strip()

    kwargs = job.kwargs or {}
    config_dict = kwargs.get("config_dict")
    if isinstance(config_dict, dict):
        cfg_experiment = config_dict.get("experiment")
        if isinstance(cfg_experiment, str) and cfg_experiment.strip():
            return cfg_experiment.strip()
    return None


def is_job_for_experiment(job: "rq.job.Job", experiment_name: str | None) -> bool:
    """Return True when job belongs to *experiment_name* (or when no filter given)."""
    if experiment_name is None:
        return True
    return get_job_experiment_name(job) == experiment_name


def validate_queue_name_component(name: str) -> str:
    """Validate that a name is safe for use in Redis queue names.

    Queue names are built as ``crsbench_{experiment_name}_build`` etc.
    This validates the user-supplied component to prevent injection of
    unexpected characters into queue names.

    Args:
        name: Experiment or queue name component

    Returns:
        The validated name (unchanged)

    Raises:
        ValueError: If name contains invalid characters
    """
    if not _VALID_QUEUE_COMPONENT.match(name):
        raise ValueError(
            f"Invalid name for queue component: {name!r}. "
            "Must start with alphanumeric and contain only [a-zA-Z0-9_-]."
        )
    return name


def get_queue_model() -> str:
    """Return queue model for distributed runtime.

    Supported values:
    - ``flat`` (default): global trial/build/verify queues shared by all experiments
    - ``per-experiment``: legacy per-experiment queue names
    """
    model = os.environ.get(QUEUE_MODEL_ENV, QUEUE_MODEL_FLAT).strip().lower()
    if model not in {QUEUE_MODEL_FLAT, QUEUE_MODEL_PER_EXPERIMENT}:
        logger.warning(
            f"Invalid {QUEUE_MODEL_ENV}={model!r}; falling back to {QUEUE_MODEL_FLAT!r}"
        )
        return QUEUE_MODEL_FLAT
    return model


def resolve_queue_names(experiment_name: str) -> tuple[str, str, str]:
    """Resolve trial/build/verify queue names for the configured queue model."""
    if get_queue_model() == QUEUE_MODEL_FLAT:
        return FLAT_TRIAL_QUEUE, FLAT_BUILD_QUEUE, FLAT_VERIFY_QUEUE

    validate_queue_name_component(experiment_name)
    return (
        f"crsbench_{experiment_name}",
        f"crsbench_{experiment_name}_build",
        f"crsbench_{experiment_name}_verify",
    )


# Cache for auth detection: avoids extra connection attempt per call.
# None = not determined yet, True = password required, False = no password.
_auth_required: Optional[bool] = None


class RedisConnectionProbe(StrEnum):
    """Outcome classification for Redis connection checks."""

    READY = "ready"
    RETRYABLE = "retryable"
    FATAL = "fatal"


def _resolve_redis_endpoint(redis_host: str) -> tuple[str, int]:
    """Resolve Redis host/port from host string and environment.

    Supported formats:
    - ``redis_host="host"`` (defaults to port 6379)
    - ``redis_host="host:port"``
    """
    host = redis_host.strip()
    port = 6379

    if ":" in host:
        candidate_host, candidate_port = host.rsplit(":", 1)
        if candidate_port.isdigit():
            host = candidate_host
            port = int(candidate_port)

    return host, port


def create_redis_connection(
    redis_host: str,
    *,
    socket_connect_timeout: int = 5,
    redis_password: str | None = None,
) -> "redis.Redis":
    """Create a Redis connection with auto-detection of password requirement.

    Reads CRSBENCH_REDIS_PASSWORD from environment. If set but the server doesn't
    require auth (stale .env), retries without password. If not set but
    the server requires auth, raises a clear error.

    Caches the auth detection result so subsequent calls skip the
    unnecessary auth probe.

    Args:
        redis_host: Redis server hostname or IP address
        socket_connect_timeout: Connection timeout in seconds
        redis_password: Optional explicit Redis password override. Falls back to
            ``CRSBENCH_REDIS_PASSWORD`` when unset.

    Returns:
        Connected and pinged Redis client

    Raises:
        redis.AuthenticationError: If server requires a password but
            CRSBENCH_REDIS_PASSWORD is not set
        redis.ConnectionError: If server is unreachable
    """
    global _auth_required  # noqa: PLW0603

    if not REDIS_AVAILABLE:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed execution. "
            "Install with: pip install redis rq"
        )

    resolved_host, resolved_port = _resolve_redis_endpoint(redis_host)
    password = redis_password
    if password is None:
        password = os.environ.get("CRSBENCH_REDIS_PASSWORD") or None

    # Fast path: use cached auth decision
    if _auth_required is True and password:
        conn = redis.Redis(
            host=resolved_host,
            port=resolved_port,
            password=password,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_connect_timeout,
        )
        conn.ping()
        return conn
    if _auth_required is False:
        conn = redis.Redis(
            host=resolved_host,
            port=resolved_port,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_connect_timeout,
        )
        conn.ping()
        return conn

    # First call: probe auth requirement
    if password:
        try:
            conn = redis.Redis(
                host=resolved_host,
                port=resolved_port,
                password=password,
                socket_connect_timeout=socket_connect_timeout,
                socket_timeout=socket_connect_timeout,
            )
            conn.ping()
            _auth_required = True
            return conn
        except redis.AuthenticationError:
            # Server doesn't need auth — stale CRSBENCH_REDIS_PASSWORD in env
            logger.info(
                "CRSBENCH_REDIS_PASSWORD is set but server requires no auth, "
                "connecting without password"
            )
            conn = redis.Redis(
                host=resolved_host,
                port=resolved_port,
                socket_connect_timeout=socket_connect_timeout,
                socket_timeout=socket_connect_timeout,
            )
            conn.ping()
            _auth_required = False
            return conn

    # No password in env — connect without
    try:
        conn = redis.Redis(
            host=resolved_host,
            port=resolved_port,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_connect_timeout,
        )
        conn.ping()
        _auth_required = False
        return conn
    except redis.AuthenticationError:
        raise redis.AuthenticationError(
            "Redis server requires authentication. Set CRSBENCH_REDIS_PASSWORD environment variable."
        ) from None


def probe_redis_connection(
    redis_host: str,
    timeout: int = 2,
    *,
    redis_password: str | None = None,
) -> tuple[RedisConnectionProbe, str | None]:
    """Classify Redis probe results for startup retry logic."""
    if not REDIS_AVAILABLE:
        detail = "Redis and RQ packages are required for distributed execution."
        logger.warning(detail)
        return RedisConnectionProbe.FATAL, detail

    connection = None
    try:
        connection = create_redis_connection(
            redis_host,
            socket_connect_timeout=timeout,
            redis_password=redis_password,
        )
        logger.debug(f"Redis server at {redis_host} is reachable")
        return RedisConnectionProbe.READY, None
    except redis.AuthenticationError as exc:
        logger.error(f"Redis authentication failed for {redis_host}: {exc}")
        return RedisConnectionProbe.FATAL, str(exc)
    except (redis.ConnectionError, redis.TimeoutError) as exc:
        logger.debug(f"Redis server at {redis_host} is not reachable: {exc}")
        return RedisConnectionProbe.RETRYABLE, str(exc)
    except Exception as exc:
        logger.warning(f"Unexpected Redis probe error for {redis_host}: {exc}")
        return RedisConnectionProbe.FATAL, str(exc)
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


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
    probe_state, _detail = probe_redis_connection(redis_host, timeout=timeout)
    return probe_state is RedisConnectionProbe.READY


def wait_for_redis_connection(
    redis_host: str,
    *,
    redis_password: str | None = None,
    timeout_sec: int,
    poll_interval_sec: float = 5.0,
    probe_timeout_sec: int = 2,
) -> None:
    """Wait until Redis responds successfully or fail on timeout/fatal errors."""
    deadline = time.monotonic() + float(timeout_sec)
    last_detail = "Redis did not become ready"

    while True:
        probe_state, detail = probe_redis_connection(
            redis_host,
            timeout=probe_timeout_sec,
            redis_password=redis_password,
        )
        if probe_state is RedisConnectionProbe.READY:
            return
        if probe_state is RedisConnectionProbe.FATAL:
            raise RuntimeError(
                f"Failed to connect to Redis at {redis_host}: "
                f"{detail or 'fatal Redis probe error'}"
            )

        last_detail = detail or last_detail
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out waiting for Redis at {redis_host} after "
                f"{timeout_sec}s: {last_detail}"
            )
        time.sleep(min(poll_interval_sec, remaining))


def initialize_queue(
    redis_host: str,
    experiment_name: str,
    *,
    redis_password: str | None = None,
) -> Optional["rq.Queue"]:
    """
    Initialize Redis-backed RQ queue for an experiment.

    Args:
        redis_host: Redis server hostname or IP address
        experiment_name: Experiment identifier for queue naming
        redis_password: Optional explicit Redis password override. Falls back to
            ``CRSBENCH_REDIS_PASSWORD`` when unset.

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

    trial_queue_name, _build_queue_name, _verify_queue_name = resolve_queue_names(
        experiment_name
    )
    queue_name = trial_queue_name
    logger.info(f"Initializing queue: {queue_name}")

    try:
        redis_connection = create_redis_connection(
            redis_host,
            redis_password=redis_password,
        )

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
        str: Unique trial key in format
            "crs:benchmark:harness:mode:sanitizer:trial_num:target_cpv_id"

    Example:
        >>> key = get_trial_key(job)
        >>> print(key)  # "atlantis-c:libxml2:fuzz_test:delta:1"
    """
    meta = job.meta or {}
    crs = meta.get("crs")
    benchmark = meta.get("benchmark")
    harness = meta.get("harness")
    if isinstance(crs, str) and isinstance(benchmark, str) and isinstance(harness, str):
        return build_trial_key(
            crs=crs,
            benchmark=benchmark,
            harness=harness,
            mode=meta.get("mode", "-"),
            sanitizer=meta.get("sanitizer", "-"),
            trial_num=meta.get("trial_num", "-"),
            target_cpv_id=meta.get("target_cpv_id"),
        )
    experiment = get_job_experiment_name(job) or "-"
    return f"job:{experiment}:{job.id or 'unknown'}"


def get_existing_trial_jobs(
    queue: "rq.Queue", experiment_name: str | None = None
) -> dict[str, list["rq.job.Job"]]:
    """Get all existing physical jobs grouped by queue status.

    Unlike ``get_existing_trials()``, this preserves every matching RQ job even
    when multiple physical jobs share the same logical ``trial_key``.
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    result: dict[str, list["rq.job.Job"]] = {
        "queued": [],
        "started": [],
        "deferred": [],
        "scheduled": [],
        "finished": [],
        "failed": [],
    }

    try:
        for job in get_all_jobs(queue):
            if job.is_queued and is_job_for_experiment(job, experiment_name):
                result["queued"].append(job)

        for bucket_name, registry in (
            ("started", queue.started_job_registry),
            ("finished", queue.finished_job_registry),
            ("deferred", queue.deferred_job_registry),
            ("failed", queue.failed_job_registry),
        ):
            for job_id in registry.get_job_ids():
                try:
                    job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                    if job and is_job_for_experiment(job, experiment_name):
                        result[bucket_name].append(job)
                except Exception as e:
                    logger.warning(f"Failed to fetch {bucket_name} job {job_id}: {e}")

        if hasattr(queue, "scheduled_job_registry"):
            for job_id in queue.scheduled_job_registry.get_job_ids():
                try:
                    job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
                    if job and is_job_for_experiment(job, experiment_name):
                        result["scheduled"].append(job)
                except Exception as e:
                    logger.warning(f"Failed to fetch scheduled job {job_id}: {e}")

        return result
    except Exception as e:
        logger.error(f"Failed to get existing physical jobs: {e}")
        return result


def get_existing_trials(
    queue: "rq.Queue", experiment_name: str | None = None
) -> dict[str, dict[str, "rq.job.Job"]]:
    """
    Get all existing jobs grouped by status with trial keys.

    Args:
        queue: RQ queue instance
        experiment_name: Optional experiment filter (required for flat queue safety)

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
    result: dict[str, dict[str, "rq.job.Job"]] = {
        "queued": {},
        "started": {},
        "deferred": {},
        "scheduled": {},
        "finished": {},
        "failed": {},
    }

    physical_jobs = get_existing_trial_jobs(queue, experiment_name=experiment_name)
    for bucket_name, jobs in physical_jobs.items():
        duplicate_keys: set[str] = set()
        for job in jobs:
            trial_key = get_trial_key(job)
            if trial_key in result[bucket_name]:
                duplicate_keys.add(trial_key)
            result[bucket_name][trial_key] = job
        if duplicate_keys:
            duplicate_list = ", ".join(sorted(duplicate_keys))
            logger.warning(
                f"Detected duplicate logical trial keys in {bucket_name} registry: "
                f"{duplicate_list}"
            )
    return result


def remove_job_by_id(queue: "rq.Queue", job_id: str) -> bool:
    """Remove a job reference from queue registries and delete payload."""
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    removed = False
    registries = [
        queue.started_job_registry,
        queue.finished_job_registry,
        queue.failed_job_registry,
        queue.deferred_job_registry,
    ]
    if hasattr(queue, "scheduled_job_registry"):
        registries.append(queue.scheduled_job_registry)

    for registry in registries:
        try:
            registry.remove(job_id, delete_job=False)
            removed = True
        except Exception as e:
            logger.debug(
                f"Registry remove skipped for job {job_id} in {type(registry).__name__}: {e}"
            )

    try:
        queue.remove(job_id)
        removed = True
    except Exception as e:
        logger.debug(f"Queue remove skipped for job {job_id}: {e}")

    try:
        job = rq.job.Job.fetch(job_id, connection=queue.connection)  # type: ignore[attr-defined]
        job.delete()
        removed = True
    except Exception as e:
        logger.debug(f"Job payload delete skipped for job {job_id}: {e}")

    return removed


def clear_experiment_jobs(queue: "rq.Queue", experiment_name: str) -> int:
    """Remove all jobs for one experiment from a queue (all registries/states)."""
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    existing = get_existing_trial_jobs(queue, experiment_name=experiment_name)
    job_ids: set[str] = set()
    for jobs in existing.values():
        for job in jobs:
            if job.id:
                job_ids.add(job.id)

    removed = 0
    for job_id in sorted(job_ids):
        if remove_job_by_id(queue, job_id):
            removed += 1

    if removed > 0:
        logger.info(
            f"Cleared {removed} jobs from queue {queue.name} for experiment={experiment_name}"
        )
    return removed


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
    queue: "rq.Queue",
    started_jobs: dict[str, "rq.job.Job"] | list["rq.job.Job"],
    *,
    lifecycle_store: JobLifecycleStore | None = None,
    stale_started_grace_seconds: int = 120,
) -> int:
    """
    Move orphaned started jobs to failed and requeue for retry.

    Orphaned jobs are started jobs with no workers connected to this queue
    (workers died mid-execution).

    Args:
        queue: RQ queue instance
        started_jobs: Started jobs to recover. Accepts either the grouped
            ``trial_key -> Job`` mapping or a physical list of jobs.

    Returns:
        int: Number of orphaned jobs handled

    Example:
        >>> existing = get_existing_trials(queue)
        >>> count = handle_orphaned_jobs(queue, existing['started'])
        >>> print(f"Handled {count} orphaned jobs")
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    jobs = list(
        started_jobs.values() if isinstance(started_jobs, dict) else started_jobs
    )
    count = 0
    requeued_count = 0
    removed_duplicate_count = 0
    active_jobs_by_experiment: dict[str | None, dict[str, list["rq.job.Job"]]] = {}
    stale_started_ids_by_trial: dict[tuple[str | None, str], set[str]] = {}
    survivor_started_job_id_by_trial: dict[tuple[str | None, str], str] = {}

    for job in jobs:
        job_id = getattr(job, "id", None)
        if not isinstance(job_id, str) or not job_id:
            continue
        trial_bucket = (get_job_experiment_name(job), get_trial_key(job))
        stale_started_ids_by_trial.setdefault(trial_bucket, set()).add(job_id)

    for trial_bucket, job_ids in stale_started_ids_by_trial.items():
        survivor_started_job_id_by_trial[trial_bucket] = sorted(job_ids)[0]

    for job in jobs:
        if not _is_stale_started_job(job, stale_started_grace_seconds):
            continue
        experiment_name = get_job_experiment_name(job)
        trial_key = get_trial_key(job)
        trial_bucket = (experiment_name, trial_key)
        if experiment_name not in active_jobs_by_experiment:
            active_jobs_by_experiment[experiment_name] = get_existing_trial_jobs(
                queue, experiment_name=experiment_name
            )
        if _has_other_active_trial_job(
            job,
            active_jobs_by_experiment[experiment_name],
            stale_started_ids_by_trial.get(trial_bucket, set()),
        ):
            if remove_job_by_id(queue, job.id):
                count += 1
                removed_duplicate_count += 1
                logger.warning(
                    f"Removed stale duplicate started job {job.id[:8]} for logical "
                    f"trial {trial_key}"
                )
            continue
        if (
            len(stale_started_ids_by_trial.get(trial_bucket, set())) > 1
            and survivor_started_job_id_by_trial.get(trial_bucket) != job.id
        ):
            if remove_job_by_id(queue, job.id):
                count += 1
                removed_duplicate_count += 1
                logger.warning(
                    f"Removed extra stale started duplicate {job.id[:8]} for logical "
                    f"trial {trial_key}; survivor="
                    f"{survivor_started_job_id_by_trial[trial_bucket][:8]}"
                )
            continue
        try:
            preparation = _prepare_requeued_started_job_lifecycle(
                job,
                lifecycle_store=lifecycle_store,
            )
            if preparation == "skip":
                continue
            if preparation == "discard":
                if remove_job_by_id(queue, job.id):
                    count += 1
                    logger.warning(
                        "Removed terminal started-job residue %s for logical trial %s",
                        job.id[:8],
                        trial_key,
                    )
                continue
            if preparation == "quarantine":
                job.set_status(rq.job.JobStatus.FAILED)  # type: ignore[attr-defined]
                count += 1
                logger.warning(
                    "Quarantined stale started job %s after lifecycle repair failure",
                    job.id[:8],
                )
                continue
            # Move to failed registry
            job.set_status(rq.job.JobStatus.FAILED)  # type: ignore[attr-defined]
            # Requeue for retry
            queue.enqueue_job(job)
            count += 1
            requeued_count += 1
            logger.debug(f"Handled orphaned job {job.id[:8]}")
        except Exception as e:
            _rollback_requeued_started_job_lifecycle(
                job,
                reason=str(e),
                lifecycle_store=lifecycle_store,
            )
            logger.warning(f"Failed to handle orphaned job {job.id[:8]}: {e}")

    if count > 0:
        logger.info(
            f"Handled {count} orphaned jobs ({requeued_count} requeued, "
            f"{removed_duplicate_count} stale duplicates removed)"
        )
    return count


def _prepare_requeued_started_job_lifecycle(
    job: "rq.job.Job",
    *,
    lifecycle_store: JobLifecycleStore | None,
) -> Literal["ready", "skip", "discard", "quarantine"]:
    """Repair lifecycle ownership before continue-mode stale started-job requeue."""
    experiment_name = get_job_experiment_name(job)
    job_id = getattr(job, "id", None)
    if not isinstance(experiment_name, str) or not experiment_name:
        return "ready"
    if not isinstance(job_id, str) or not job_id:
        return "ready"

    if lifecycle_store is None:
        return "ready"

    try:
        record = lifecycle_store.get(experiment_name, job_id)
        if record is None:
            return "ready"
        if record.state in {JobState.COMPLETED, JobState.FAILED}:
            return "discard"

        if record.state in {JobState.CLAIMED, JobState.RUNNING, JobState.SYNCING}:
            record = lifecycle_store.transition(
                experiment_name,
                job_id,
                JobState.ORPHANED,
                claimed_by=None,
                detail="recovered stale started job",
            )

        if record.state is JobState.ORPHANED:
            retry_count = lifecycle_store.increment_retry(experiment_name, job_id)
            meta = getattr(job, "meta", None)
            if isinstance(meta, dict):
                meta["retry_count"] = retry_count
                meta.pop("worker_name", None)
                save_meta = getattr(job, "save_meta", None)
                if callable(save_meta):
                    save_meta()

        lifecycle_store.transition(
            experiment_name,
            job_id,
            JobState.QUEUED,
            claimed_by=None,
            detail="recovered stale started job",
        )
    except Exception as exc:
        _rollback_requeued_started_job_lifecycle(
            job,
            reason=f"retry metadata update failed: {exc}",
            lifecycle_store=lifecycle_store,
        )
        logger.warning(
            "Failed to repair lifecycle for recovered stale started job %s: %s",
            job_id[:8],
            exc,
        )
        return "quarantine"
    return "ready"


def _rollback_requeued_started_job_lifecycle(
    job: "rq.job.Job",
    *,
    reason: str,
    lifecycle_store: JobLifecycleStore | None,
) -> None:
    """Restore a terminal lifecycle state when started-job requeue fails."""
    experiment_name = get_job_experiment_name(job)
    job_id = getattr(job, "id", None)
    if not isinstance(experiment_name, str) or not experiment_name:
        return
    if not isinstance(job_id, str) or not job_id:
        return

    if lifecycle_store is None:
        return

    try:
        record = lifecycle_store.get(experiment_name, job_id)
        if record is None or record.state is JobState.COMPLETED:
            return
        lifecycle_store.transition(
            experiment_name,
            job_id,
            JobState.FAILED,
            claimed_by=None,
            detail=f"started-job retry enqueue failed: {reason}",
        )
    except Exception as exc:
        logger.warning(
            "Failed to roll back lifecycle for recovered stale started job %s: %s",
            job_id[:8],
            exc,
        )


def _has_other_active_trial_job(
    job: "rq.job.Job",
    existing: dict[str, list["rq.job.Job"]],
    sibling_started_ids: set[str],
) -> bool:
    """Return whether another active physical job already exists for this trial."""
    job_id = getattr(job, "id", None)
    if not isinstance(job_id, str) or not job_id:
        return False

    trial_key = get_trial_key(job)
    for bucket_name in ("queued", "deferred", "scheduled", "started"):
        for other in existing[bucket_name]:
            other_id = getattr(other, "id", None)
            if not isinstance(other_id, str) or other_id == job_id:
                continue
            if get_trial_key(other) != trial_key:
                continue
            if bucket_name != "started" or other_id not in sibling_started_ids:
                return True
    return False


def _is_stale_started_job(
    job: "rq.job.Job",
    stale_started_grace_seconds: int,
) -> bool:
    """Return whether a started job has exceeded timeout plus grace."""
    status_value = getattr(job.get_status(), "value", job.get_status())
    if str(status_value).lower() != "started":
        return False

    started_at = getattr(job, "started_at", None)
    timeout_seconds = int(getattr(job, "timeout", 3600) or 3600)
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return age_seconds > (timeout_seconds + stale_started_grace_seconds)


def _normalize_queue_name(name: object) -> str | None:
    """Convert queue name values from RQ/Redis formats to a normalized string."""
    if isinstance(name, bytes):
        return name.decode("utf-8", errors="ignore")
    if isinstance(name, str):
        return name
    return None
