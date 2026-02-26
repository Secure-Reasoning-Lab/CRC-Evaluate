"""Worker implementation for CRSBench distributed execution.

This module implements the worker process that connects to the Redis queue,
pulls jobs, executes them, and reports results back to the queue.

Workers handle trial jobs only. For CI build/verify jobs, use the evaluator
with --ci mode instead.

A file-based lock ensures only one worker process runs at a time.
"""

import fcntl
import multiprocessing
import os
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from crsbench.distributed.common import (
    collect_validated_int_metadata,
    discover_registered_experiments,
    normalize_cpu_tag,
    normalize_redis_host,
    resolve_cli_or_first_metadata,
    validate_optional_int_override,
)
from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.utils.logger import configure_logger, get_logger

# Load environment variables from .env file if present
load_dotenv()

# Worker lock file configuration
DEFAULT_LOCK_DIR = "/tmp"
LOCK_DIR = Path(os.environ.get("CRSBENCH_WORKER_LOCK_DIR", DEFAULT_LOCK_DIR))

logger = get_logger(__name__)

# Default CPU cores allocated per trial job when using the ci_supervisor
DEFAULT_TRIAL_CORES_PER_JOB = 4


@contextmanager
def worker_lock(worker_name: str):
    """Acquire an exclusive lock to ensure only one worker process runs at a time.

    This context manager uses file-based locking (fcntl) to prevent concurrent
    worker processes from running simultaneously. The lock is non-blocking and
    will raise BlockingIOError immediately if another worker is already running.

    Args:
        worker_name: Worker name used to generate unique lock file path

    Raises:
        BlockingIOError: If another worker process already holds the lock
        OSError: If lock file cannot be created or accessed

    Example:
        with worker_lock("worker-0"):
            # Only one worker process with this name will execute this block at a time
            worker.work()
    """
    # Generate unique lock file path based on worker name
    lock_file_path = LOCK_DIR / f"crsbench-worker-{worker_name}.lock"

    lock_file = None
    try:
        # Ensure parent directory exists
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Open lock file
        lock_file = lock_file_path.open("w")

        # Acquire exclusive lock (non-blocking)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Lock acquired successfully
        yield

    finally:
        # Release lock and cleanup
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except (OSError, ValueError):
                pass  # File might already be closed or deleted


def _trial_job_runner(
    redis_host: str,
    child_name: str,
    job_id: str,
) -> None:
    """Adapter for ci_supervisor: delegates to evaluator's _run_single_job."""
    from crsbench.distributed.evaluator import _run_single_job

    _run_single_job(redis_host, job_id, child_name=child_name, execution_role="worker")


def main(
    redis_host: Optional[str] = None,
    experiment_name: Optional[str] = None,
    worker_name: Optional[str] = None,
    num_workers: int = 1,
    queue_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    cores_per_job: int = DEFAULT_TRIAL_CORES_PER_JOB,
    log_level: str = "INFO",
) -> int:
    """
    Worker entry point - connects to Redis and processes trial jobs.

    Args:
        redis_host: Redis server hostname (overrides CRSBENCH_REDIS_HOST env var)
        experiment_name: Experiment identifier for queue naming
        worker_name: Worker name for identification
        num_workers: Number of parallel worker processes
        queue_name: Redis queue name
        use_cpuset: Enable CPU affinity
        cores: CPU cores for worker pool
        skip_cpus: CPUs to exclude from allocation
        log_level: Worker logging level (default: INFO)

    Environment Variables (used when CLI args not provided):
        CRSBENCH_REDIS_HOST: Redis server hostname (default: localhost)

    Returns:
        Exit code (0 for success, non-zero for errors)

    Exit Codes:
        0: Normal shutdown (queue empty)
        1: Redis/RQ not installed
        2: Cannot connect to Redis
        3: Worker error
        4: Another worker already running
    """
    # Configure logging (if not already configured by caller)
    configure_logger(level=log_level, sink=sys.stdout)

    # Check if Redis/RQ available
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for worker execution")
        logger.error("Install with: pip install redis rq")
        sys.exit(1)

    # Resolve runtime configuration.
    normalized_cli_redis_host = normalize_redis_host(redis_host)
    if redis_host is not None and normalized_cli_redis_host is None:
        logger.error(
            "Invalid redis host override for worker. "
            "Set redis_host to a non-empty hostname."
        )
        return 1
    if normalized_cli_redis_host is not None:
        redis_host = normalized_cli_redis_host
    else:
        env_redis_host = os.environ.get("CRSBENCH_REDIS_HOST")
        if env_redis_host is not None:
            redis_host = normalize_redis_host(env_redis_host)
            if redis_host is None:
                logger.error(
                    "Invalid CRSBENCH_REDIS_HOST for worker. "
                    "Set it to a non-empty hostname."
                )
                return 1
        else:
            redis_host = "localhost"
    experiment_name = experiment_name or "default"
    worker_name = worker_name or socket.gethostname()

    from crsbench.distributed.queue import (
        resolve_queue_names,
        validate_queue_name_component,
    )

    validate_queue_name_component(experiment_name)

    # Resolve queue name
    default_trial_queue, _build_queue, _verify_queue = resolve_queue_names(
        experiment_name
    )
    queue_name = queue_name or default_trial_queue

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Worker")
    logger.info("=" * 60)
    logger.info(f"Worker name: {worker_name}")
    logger.info(f"Parallel workers: {num_workers}")
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Queue: {queue_name}")
    logger.info("=" * 60)

    # Single worker mode (use lock)
    if num_workers == 1:
        try:
            with worker_lock(worker_name):
                _run_worker(redis_host, experiment_name, worker_name, queue_name)
            return 0
        except BlockingIOError:
            lock_file_path = LOCK_DIR / f"crsbench-worker-{worker_name}.lock"
            logger.error("=" * 60)
            logger.error("Another worker is already running")
            logger.error(f"Lock file: {lock_file_path}")
            logger.error("=" * 60)
            return 4

    # Multi-worker mode (no lock, spawn multiple processes)
    if use_cpuset:
        # Use ci_supervisor for CPU affinity
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        return run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name=queue_name,
            verify_queue_name=queue_name,
            worker_name=worker_name,
            build_jobs=num_workers,
            build_cores_per_job=cores_per_job,
            verify_jobs=0,
            job_runner=_trial_job_runner,
            use_cpuset=True,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            cpu_tag=cpu_tag,
        )

    # Standard parallel workers
    return _spawn_workers(
        redis_host,
        experiment_name,
        worker_name,
        num_workers,
        queue_name=queue_name,
        continuous=False,
        log_level=log_level,
    )


def _spawn_workers(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    num_workers: int,
    queue_name: Optional[str] = None,
    *,
    continuous: bool = False,
    log_level: str = "INFO",
) -> int:
    """Spawn multiple worker processes.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        worker_name: Base worker name
        num_workers: Number of worker processes to spawn
        queue_name: Redis queue name (default: crsbench_{experiment_name})
        continuous: Run in continuous mode

    Returns:
        Exit code (0 for success)
    """
    from crsbench.distributed.queue import resolve_queue_names

    default_trial_queue, _build_queue, _verify_queue = resolve_queue_names(
        experiment_name
    )
    queue_name = queue_name or default_trial_queue
    logger.info(f"Spawning {num_workers} worker processes...")

    processes = []
    try:
        for i in range(num_workers):
            # Generate unique worker name
            name = f"{worker_name}-{i}"

            # Create worker process
            p = multiprocessing.Process(
                target=_run_single_worker,
                args=(redis_host, experiment_name, name),
                kwargs={
                    "continuous": continuous,
                    "queue_name": queue_name,
                    "log_level": log_level,
                },
                name=f"worker-{i}",
            )
            p.start()
            processes.append(p)
            logger.info(f"Started worker process '{name}' (PID: {p.pid})")

        # Wait for all workers
        logger.info("Waiting for worker processes to complete...")
        for p in processes:
            p.join()

        # Check exit codes
        failed = [p for p in processes if p.exitcode != 0]
        if failed:
            logger.error(f"{len(failed)}/{num_workers} worker processes failed")
            return 3

        logger.info(f"All {num_workers} worker processes completed successfully")
        return 0

    except KeyboardInterrupt:
        logger.info("\nReceived interrupt signal, terminating workers...")
        for p in processes:
            if p.is_alive():
                p.terminate()
        for p in processes:
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing worker {p.name}")
                p.kill()
                p.join()
        return 0


def _run_single_worker(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    *,
    continuous: bool = False,
    queue_name: Optional[str] = None,
    log_level: str = "INFO",
):
    """Run a single worker process (for multiprocessing).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        worker_name: Worker name
        continuous: Run in continuous mode
        queue_name: Redis queue name (default: crsbench_{experiment_name})
    """
    # Note: This runs in a subprocess, so we need to reconfigure logging
    configure_logger(level=log_level, sink=sys.stdout)

    if continuous:
        # Run a single continuous worker loop in this subprocess.
        _run_worker_continuous(redis_host, experiment_name, worker_name, queue_name)
    else:
        # Run burst mode worker
        _run_worker(redis_host, experiment_name, worker_name, queue_name)


def _run_worker(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    queue_name: Optional[str] = None,
):
    """Internal helper to run the worker (separated for lock management).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming
        worker_name: Worker name for identification
        queue_name: Redis queue name (default: crsbench_{experiment_name})
    """
    import rq

    # Connect to Redis
    logger.info(f"Connecting to Redis at {redis_host}...")
    from crsbench.distributed.queue import create_redis_connection

    redis_connection = create_redis_connection(redis_host)
    logger.info("Connected to Redis successfully")

    # Set up RQ queue and worker (RQ 2.x requires explicit connection)
    from crsbench.distributed.queue import resolve_queue_names

    default_trial_queue, _build_queue, _verify_queue = resolve_queue_names(
        experiment_name
    )
    queue_name = queue_name or default_trial_queue
    queue = rq.Queue(queue_name, connection=redis_connection)  # type: ignore[attr-defined]

    # Store friendly worker name in environment for job metadata
    os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = worker_name

    # Let RQ generate unique worker name to avoid conflicts
    worker = rq.Worker([queue], connection=redis_connection)  # type: ignore[attr-defined]

    logger.info(f"Worker '{worker_name}' started, listening on queue: {queue_name}")
    logger.info("Waiting for jobs...")

    # Work in burst mode until queue is empty
    # Burst mode: process available jobs then exit (vs. continuous polling)
    while queue.count + queue.deferred_job_registry.count > 0:
        logger.debug(
            f"Queue status: {queue.count} queued, "
            f"{queue.deferred_job_registry.count} deferred"
        )

        # Process jobs with timeout
        worker.work(
            burst=True,  # Exit after processing available jobs
            max_jobs=1,  # Process one job at a time for better logging
        )

        # Brief sleep to allow queue state to update
        time.sleep(2)

    logger.info("=" * 60)
    logger.info("Queue empty, worker shutting down")
    logger.info("=" * 60)


def _run_worker_continuous(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    queue_name: Optional[str] = None,
) -> None:
    """Internal helper to run one RQ worker in continuous polling mode."""
    import rq

    logger.info(f"Connecting to Redis at {redis_host}...")
    from crsbench.distributed.queue import create_redis_connection

    redis_connection = create_redis_connection(redis_host)
    logger.info("Connected to Redis successfully")

    from crsbench.distributed.queue import resolve_queue_names

    default_trial_queue, _build_queue, _verify_queue = resolve_queue_names(
        experiment_name
    )
    queue_name = queue_name or default_trial_queue
    queue = rq.Queue(queue_name, connection=redis_connection)  # type: ignore[attr-defined]

    os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = worker_name
    worker = rq.Worker([queue], connection=redis_connection)  # type: ignore[attr-defined]

    logger.info(
        f"Continuous worker '{worker_name}' started, listening on queue: {queue_name}"
    )
    logger.info("Polling for jobs...")
    worker.work(burst=False)


def run_worker_continuous(
    redis_host: str,
    experiment_name: str,
    worker_name: Optional[str] = None,
    num_workers: int = 1,
    queue_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    cores_per_job: int = DEFAULT_TRIAL_CORES_PER_JOB,
    log_level: str = "INFO",
):
    """
    Run worker in continuous mode (polling indefinitely).

    Unlike the main() function which runs in burst mode, this function
    runs continuously and never exits (except on error or interrupt).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming
        worker_name: Worker name for identification (default: hostname)
        num_workers: Number of worker processes to spawn (default: 1)
        queue_name: Redis queue name
        use_cpuset: Enable CPU affinity
        minimum_disk_size: Minimum free disk space before pausing
        disk_check_interval: Seconds between disk space checks
        cores: CPU cores for worker pool
        skip_cpus: CPUs to exclude from allocation

    Note:
        This mode is useful for long-running worker deployments where
        the worker should continuously process jobs as they arrive.
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    worker_name = worker_name or socket.gethostname()

    from crsbench.distributed.queue import (
        resolve_queue_names,
        validate_queue_name_component,
    )

    validate_queue_name_component(experiment_name)

    # Resolve queue name
    default_trial_queue, _build_queue, _verify_queue = resolve_queue_names(
        experiment_name
    )
    queue_name = queue_name or default_trial_queue

    # Always use supervisor mode for consistent behavior
    if use_cpuset:
        # Use ci_supervisor for CPU affinity
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        logger.info(
            f"Starting supervisor with {num_workers} workers and CPU affinity for: {experiment_name}"
        )
        exit_code = run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name=queue_name,
            verify_queue_name=queue_name,
            worker_name=worker_name,
            build_jobs=num_workers,
            build_cores_per_job=cores_per_job,
            verify_jobs=0,
            job_runner=_trial_job_runner,
            use_cpuset=True,
            use_cgroups=use_cpuset,
            minimum_disk_size=minimum_disk_size,
            disk_check_interval=disk_check_interval,
            cores=cores,
            skip_cpus=skip_cpus,
            cpu_tag=cpu_tag,
        )
    else:
        logger.info(
            f"Starting {num_workers} continuous workers for experiment: {experiment_name}"
        )
        logger.info(f"Base worker name: {worker_name}")
        exit_code = _spawn_workers(
            redis_host,
            experiment_name,
            worker_name,
            num_workers,
            queue_name=queue_name,
            continuous=True,
            log_level=log_level,
        )

    if exit_code != 0:
        raise RuntimeError(f"Worker processes failed with exit code {exit_code}")


def run_worker_configless(
    redis_host: str = "localhost",
    worker_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    continuous: bool = True,
    jobs_override: Optional[int] = None,
    cores_per_job_override: Optional[int] = None,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    log_level: str = "INFO",
) -> int:
    """Run worker in configless mode — discover experiments from Redis registry.

    Instead of requiring ``--experiment-config``, this function connects to
    Redis, reads the experiment registry, and listens on all discovered trial
    queues.

    Args:
        redis_host: Redis server hostname.
        worker_name: Worker name for identification (default: hostname).
        use_cpuset: Enable CPU affinity.
        cores: CPU cores for worker pool.
        skip_cpus: CPUs to exclude from allocation.
        continuous: Run in continuous mode (poll for new experiments).
        minimum_disk_size: Minimum free disk space before pausing.
        disk_check_interval: Seconds between disk space checks.
        log_level: Worker logging level.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    configure_logger(level=log_level, sink=sys.stdout)

    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for worker execution")
        return 1

    normalized_redis_host = normalize_redis_host(redis_host)
    if normalized_redis_host is None:
        logger.error(
            "Configless worker requires a Redis host. "
            "Set CRSBENCH_REDIS_HOST to a non-empty hostname."
        )
        return 1
    redis_host = normalized_redis_host

    worker_name = worker_name or socket.gethostname()

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Worker (configless)")
    logger.info("=" * 60)
    logger.info(f"Worker name: {worker_name}")
    logger.info(f"Redis host: {redis_host}")
    logger.info("Discovering experiments from registry...")
    logger.info("=" * 60)

    max_poll_attempts = None if continuous else 12
    try:
        _redis_conn, experiments = discover_registered_experiments(
            redis_host,
            max_poll_attempts=max_poll_attempts,
        )
    except RuntimeError as exc:
        logger.error(f"{exc} — exiting")
        return 1

    # Use stable experiment ordering so metadata precedence is deterministic.
    ordered_regs = [experiments[name] for name in sorted(experiments)]

    # Collect all trial queue names
    queue_names = sorted({reg.trial_queue for reg in ordered_regs})
    try:
        jobs_override = validate_optional_int_override(
            value=jobs_override,
            field_name="worker.jobs",
            minimum=1,
        )
        cores_per_job_override = validate_optional_int_override(
            value=cores_per_job_override,
            field_name="worker.cores_per_job",
            minimum=1,
        )
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    try:
        metadata_jobs = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="worker_jobs",
            field_name="worker.jobs",
            minimum=1,
        )
        metadata_cores_per_job = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="worker_cores_per_job",
            field_name="worker.cores_per_job",
            minimum=1,
        )
        if not metadata_cores_per_job:
            metadata_cores_per_job = collect_validated_int_metadata(
                registrations=ordered_regs,
                attr_name="cores_per_trial",
                field_name="resources.cores_per_trial",
                minimum=1,
            )
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1
    metadata_worker_cores = [
        reg.worker_cores for reg in ordered_regs if reg.worker_cores is not None
    ]
    metadata_worker_skip = [
        reg.worker_skip_cpus for reg in ordered_regs if reg.worker_skip_cpus is not None
    ]
    metadata_worker_cpu_tags: list[str] = []
    for reg in ordered_regs:
        candidate_tag = normalize_cpu_tag(reg.worker_cpu_tag) or normalize_cpu_tag(
            reg.cpu_tag
        )
        if candidate_tag is not None:
            metadata_worker_cpu_tags.append(candidate_tag)
    resolved_jobs = (
        jobs_override
        if jobs_override is not None
        else (max(metadata_jobs) if metadata_jobs else 1)
    )
    resolved_cores_per_job = (
        cores_per_job_override
        if cores_per_job_override is not None
        else (
            max(metadata_cores_per_job)
            if metadata_cores_per_job
            else DEFAULT_TRIAL_CORES_PER_JOB
        )
    )
    resolved_cores = resolve_cli_or_first_metadata(
        cli_value=cores,
        metadata_values=metadata_worker_cores,
        field_name="worker.cores",
    )
    resolved_skip_cpus = resolve_cli_or_first_metadata(
        cli_value=skip_cpus,
        metadata_values=metadata_worker_skip,
        field_name="worker.skip_cpus",
    )
    resolved_cpu_tag: Optional[str]
    if cpu_tag is not None:
        resolved_cpu_tag = normalize_cpu_tag(cpu_tag)
    else:
        distinct_cpu_tags = sorted(set(metadata_worker_cpu_tags))
        if len(distinct_cpu_tags) > 1:
            logger.error(
                "Conflicting worker.cpu_tag/resources.cpu_tag metadata across "
                "experiments. Set --cpu-tag explicitly to run this worker."
            )
            return 1
        resolved_cpu_tag = distinct_cpu_tags[0] if distinct_cpu_tags else None
    logger.info(f"Discovered {len(experiments)} experiment(s), queues: {queue_names}")
    logger.info(
        f"Worker resource profile (CLI > metadata > default): jobs={resolved_jobs}, "
        f"cores_per_job={resolved_cores_per_job}, "
        f"cores={resolved_cores}, skip_cpus={resolved_skip_cpus}, "
        f"cpu_tag={resolved_cpu_tag}"
    )

    def _refresh_trial_queues(redis_conn):
        from crsbench.distributed.registry import RegistryClient

        registry = RegistryClient(redis_conn)
        refreshed = registry.list_experiments()
        if not refreshed:
            return [], []
        refreshed_trial_queues = sorted(
            {refreshed[name].trial_queue for name in sorted(refreshed)}
        )
        return refreshed_trial_queues, []

    # Use multi-queue supervisor for both cpuset and non-cpuset modes so
    # configless workers can dynamically adopt queues for new experiments.
    from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

    return run_multi_queue_supervisor(
        redis_host=redis_host,
        build_queue_names=queue_names,
        verify_queue_names=[],
        worker_name=worker_name,
        build_jobs=resolved_jobs,
        build_cores_per_job=resolved_cores_per_job,
        verify_jobs=0,
        job_runner=_trial_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=resolved_cores,
        skip_cpus=resolved_skip_cpus,
        minimum_disk_size=minimum_disk_size,
        disk_check_interval=disk_check_interval,
        continuous=continuous,
        queue_refresher=_refresh_trial_queues,
        cpu_tag=resolved_cpu_tag,
    )


if __name__ == "__main__":
    sys.exit(main())
