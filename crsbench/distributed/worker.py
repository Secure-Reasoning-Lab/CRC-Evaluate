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

    _run_single_job(redis_host, job_id, child_name=child_name)


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
) -> int:
    """
    Worker entry point - connects to Redis and processes trial jobs.

    Args:
        redis_host: Redis server hostname (overrides REDIS_HOST env var)
        experiment_name: Experiment identifier (overrides EXPERIMENT_NAME env var)
        worker_name: Worker name for identification
        num_workers: Number of parallel worker processes
        queue_name: Redis queue name
        use_cpuset: Enable CPU affinity
        cores: CPU cores for worker pool
        skip_cpus: CPUs to exclude from allocation

    Environment Variables (used when CLI args not provided):
        REDIS_HOST: Redis server hostname (default: localhost)
        EXPERIMENT_NAME: Experiment identifier for queue naming (default: default)
        LOG_LEVEL: Logging level (default: INFO)

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
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    configure_logger(level=log_level, sink=sys.stdout)

    # Check if Redis/RQ available
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for worker execution")
        logger.error("Install with: pip install redis rq")
        sys.exit(1)

    # Get configuration: CLI args override environment variables
    redis_host = redis_host or os.environ.get("REDIS_HOST", "localhost")
    experiment_name = experiment_name or os.environ.get("EXPERIMENT_NAME", "default")
    worker_name = (
        worker_name or os.environ.get("CRSBENCH_WORKER_NAME") or socket.gethostname()
    )

    from crsbench.distributed.queue import validate_queue_name_component

    validate_queue_name_component(experiment_name)

    # Resolve queue name
    queue_name = queue_name or f"crsbench_{experiment_name}"

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
            build_cores_per_job=DEFAULT_TRIAL_CORES_PER_JOB,
            verify_jobs=0,
            job_runner=_trial_job_runner,
            use_cpuset=True,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            continuous=False,
        )

    # Standard parallel workers
    return _spawn_workers(
        redis_host,
        experiment_name,
        worker_name,
        num_workers,
        queue_name=queue_name,
        continuous=False,
    )


def _spawn_workers(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    num_workers: int,
    queue_name: Optional[str] = None,
    *,
    continuous: bool = False,
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
    queue_name = queue_name or f"crsbench_{experiment_name}"
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
                kwargs={"continuous": continuous, "queue_name": queue_name},
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
    configure_logger(level=os.environ.get("LOG_LEVEL", "INFO").upper(), sink=sys.stdout)

    if continuous:
        # Run continuous worker (without spawning more workers)
        run_worker_continuous(
            redis_host,
            experiment_name,
            worker_name=worker_name,
            num_workers=1,
            queue_name=queue_name,
        )
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
    queue_name = queue_name or f"crsbench_{experiment_name}"
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

    worker_name = (
        worker_name or os.environ.get("CRSBENCH_WORKER_NAME") or socket.gethostname()
    )

    from crsbench.distributed.queue import validate_queue_name_component

    validate_queue_name_component(experiment_name)

    # Resolve queue name
    queue_name = queue_name or f"crsbench_{experiment_name}"

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
            build_cores_per_job=DEFAULT_TRIAL_CORES_PER_JOB,
            verify_jobs=0,
            job_runner=_trial_job_runner,
            use_cpuset=True,
            use_cgroups=use_cpuset,
            minimum_disk_size=minimum_disk_size,
            disk_check_interval=disk_check_interval,
            cores=cores,
            skip_cpus=skip_cpus,
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
        )

    if exit_code != 0:
        raise RuntimeError(f"Worker processes failed with exit code {exit_code}")


if __name__ == "__main__":
    sys.exit(main())
