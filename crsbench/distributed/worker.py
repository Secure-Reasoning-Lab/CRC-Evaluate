"""Worker implementation for CRSBench distributed execution.

This module implements the worker process that connects to the Redis queue,
pulls jobs, executes them, and reports results back to the queue.

Workers can be run locally or deployed in containers for horizontal scaling.

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

from crsbench.utils.logger import configure_logger, get_logger

# Load environment variables from .env file if present
load_dotenv()

# Worker lock file configuration
DEFAULT_LOCK_DIR = "/tmp"
LOCK_DIR = Path(os.environ.get("CRSBENCH_WORKER_LOCK_DIR", DEFAULT_LOCK_DIR))

try:
    import redis
    import rq

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = get_logger(__name__)


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


def main(
    redis_host: Optional[str] = None,
    experiment_name: Optional[str] = None,
    timeout: Optional[int] = None,
    worker_name: Optional[str] = None,
    num_workers: int = 1,
) -> int:
    """
    Worker entry point - connects to Redis and processes jobs.

    Args:
        redis_host: Redis server hostname (overrides REDIS_HOST env var)
        experiment_name: Experiment identifier (overrides EXPERIMENT_NAME env var)
        timeout: Job timeout in seconds (overrides WORKER_TIMEOUT env var)

    Environment Variables (used when CLI args not provided):
        REDIS_HOST: Redis server hostname (default: localhost)
        EXPERIMENT_NAME: Experiment identifier for queue naming (default: default)
        WORKER_TIMEOUT: Job timeout in seconds (default: 3600)
        LOG_LEVEL: Logging level (default: INFO)

    Usage:
        # Run as module
        python -m crsbench.distributed.worker

        # Run via CLI
        crsbench worker --redis-host localhost --experiment-name my-exp

        # Run in Docker
        docker run -e REDIS_HOST=redis-server -e EXPERIMENT_NAME=exp1 crsbench-worker

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
    worker_timeout = timeout or int(os.environ.get("WORKER_TIMEOUT", "3600"))
    worker_name = (
        worker_name or os.environ.get("CRSBENCH_WORKER_NAME") or socket.gethostname()
    )

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Worker")
    logger.info("=" * 60)
    logger.info(f"Worker name: {worker_name}")
    logger.info(f"Parallel workers: {num_workers}")
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Worker timeout: {worker_timeout}s")
    logger.info("=" * 60)

    # Single worker mode (use lock)
    if num_workers == 1:
        try:
            with worker_lock(worker_name):
                _run_worker(redis_host, experiment_name, worker_name)
            return 0
        except BlockingIOError:
            lock_file_path = LOCK_DIR / f"crsbench-worker-{worker_name}.lock"
            logger.error("=" * 60)
            logger.error("Another worker is already running")
            logger.error(f"Lock file: {lock_file_path}")
            logger.error("=" * 60)
            return 4

    # Multi-worker mode (no lock, spawn multiple processes)
    return _spawn_workers(
        redis_host, experiment_name, worker_name, num_workers, continuous=False
    )


def _spawn_workers(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    num_workers: int,
    *,
    continuous: bool = False,
) -> int:
    """Spawn multiple worker processes.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        worker_name: Base worker name
        num_workers: Number of worker processes to spawn
        continuous: Run in continuous mode

    Returns:
        Exit code (0 for success)
    """
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
                kwargs={"continuous": continuous},
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
    redis_host: str, experiment_name: str, worker_name: str, *, continuous: bool = False
):
    """Run a single worker process (for multiprocessing).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        worker_name: Worker name
        continuous: Run in continuous mode
    """
    # Note: This runs in a subprocess, so we need to reconfigure logging
    configure_logger(level=os.environ.get("LOG_LEVEL", "INFO").upper(), sink=sys.stdout)

    if continuous:
        # Run continuous worker (without spawning more workers)
        run_worker_continuous(
            redis_host, experiment_name, worker_name=worker_name, num_workers=1
        )
    else:
        # Run burst mode worker
        _run_worker(redis_host, experiment_name, worker_name)


def _run_worker(redis_host: str, experiment_name: str, worker_name: str):
    """Internal helper to run the worker (separated for lock management).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming
        worker_name: Worker name for identification
    """
    try:
        # Connect to Redis
        logger.info(f"Connecting to Redis at {redis_host}...")
        redis_password = os.environ.get("REDIS_PASSWORD") or None
        redis_connection = redis.Redis(
            host=redis_host,
            password=redis_password,
            socket_connect_timeout=5,
        )

        # Test connection
        redis_connection.ping()
        logger.info("✓ Connected to Redis successfully")

        # Set up RQ queue and worker (RQ 2.x requires explicit connection)
        queue_name = f"crsbench_{experiment_name}"
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

    except Exception:
        # Re-raise all exceptions to be handled by main()
        raise


def run_worker_continuous(
    redis_host: str,
    experiment_name: str,
    _timeout: int = 3600,
    worker_name: Optional[str] = None,
    num_workers: int = 1,
):
    """
    Run worker in continuous mode (polling indefinitely).

    Unlike the main() function which runs in burst mode, this function
    runs continuously and never exits (except on error or interrupt).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming
        timeout: Job execution timeout in seconds
        worker_name: Worker name for identification (default: hostname)
        num_workers: Number of worker processes to spawn (default: 1)

    Note:
        This mode is useful for long-running worker deployments where
        the worker should continuously process jobs as they arrive.
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    worker_name = (
        worker_name or os.environ.get("CRSBENCH_WORKER_NAME") or socket.gethostname()
    )

    # Single worker mode (use lock)
    if num_workers == 1:
        logger.info(f"Starting continuous worker for experiment: {experiment_name}")
        logger.info(f"Worker name: {worker_name}")

        # Acquire worker lock to ensure only one worker runs at a time
        try:
            with worker_lock(worker_name):
                redis_password = os.environ.get("REDIS_PASSWORD") or None
                redis_connection = redis.Redis(host=redis_host, password=redis_password)
                redis_connection.ping()

                # Set up RQ queue and worker (RQ 2.x requires explicit connection)
                queue_name = f"crsbench_{experiment_name}"
                queue = rq.Queue(queue_name, connection=redis_connection)  # type: ignore[attr-defined]

                # Store friendly worker name in environment for job metadata
                os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = worker_name

                # Let RQ generate unique worker name to avoid conflicts
                worker = rq.Worker([queue], connection=redis_connection)  # type: ignore[attr-defined]

                logger.info(
                    f"Worker '{worker_name}' running in continuous mode on queue: {queue_name}"
                )

                # Run worker in continuous mode (never exits)
                worker.work(
                    burst=False  # Continuous mode
                )

        except BlockingIOError:
            lock_file_path = LOCK_DIR / f"crsbench-worker-{worker_name}.lock"
            logger.error("Another worker is already running")
            logger.error(f"Lock file: {lock_file_path}")
            raise

        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            raise

    # Multi-worker mode (no lock, spawn multiple processes)
    else:
        logger.info(
            f"Starting {num_workers} continuous workers for experiment: {experiment_name}"
        )
        logger.info(f"Base worker name: {worker_name}")
        exit_code = _spawn_workers(
            redis_host, experiment_name, worker_name, num_workers, continuous=True
        )
        if exit_code != 0:
            raise RuntimeError(f"Worker processes failed with exit code {exit_code}")


if __name__ == "__main__":
    sys.exit(main())
