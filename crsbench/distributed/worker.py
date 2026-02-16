"""Worker implementation for CRSBench distributed execution.

This module implements the worker process that connects to the Redis queue,
pulls jobs, executes them, and reports results back to the queue.

Workers can be run locally or deployed in containers for horizontal scaling.

A file-based lock ensures only one worker process runs at a time.
"""

import fcntl
import multiprocessing
import os
import shutil
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

from crsbench.evaluation.results import TrialResult
from crsbench.utils.logger import configure_logger, get_logger

# Load environment variables from .env file if present
load_dotenv()

# Worker lock file configuration
DEFAULT_LOCK_DIR = "/tmp"
LOCK_DIR = Path(os.environ.get("CRSBENCH_WORKER_LOCK_DIR", DEFAULT_LOCK_DIR))

try:
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
    queue_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    ci_build_jobs: Optional[int] = None,
    ci_build_cores_per_job: Optional[int] = None,
    ci_verify_jobs: Optional[int] = None,
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
    logger.info(f"Worker timeout: {worker_timeout}s")
    logger.info("=" * 60)

    # CI dual-queue mode
    if ci_build_jobs is not None:
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        return run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name="crsbench_ci_build",
            verify_queue_name="crsbench_ci_verify",
            worker_name=worker_name,
            build_jobs=ci_build_jobs,
            build_cores_per_job=ci_build_cores_per_job or 1,
            verify_jobs=ci_verify_jobs or ci_build_jobs,
            job_runner=_ci_job_runner,
            use_cpuset=use_cpuset,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
        )

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
        # Use supervisor mode for CPU affinity
        return _run_supervisor(
            redis_host,
            experiment_name,
            worker_name,
            num_workers,
            queue_name=queue_name,
            use_cpuset=True,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
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


def _run_supervisor(
    redis_host: str,
    experiment_name: str,
    worker_name: str,
    max_workers: int,
    queue_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
) -> int:
    """Supervisor that spawns workers with fixed CPU allocation per job.

    Instead of spawning a fixed number of workers upfront, the supervisor:
    1. Peeks at next job in queue
    2. Allocates CPUs from pool (4 per job)
    3. Spawns worker with CPU affinity
    4. Cleans up and releases CPUs when worker finishes

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        worker_name: Base worker name
        max_workers: Maximum number of concurrent workers
        use_cpuset: Enable CPU affinity (default: False)
        use_cgroups: Create per-worker cgroups with cpuset constraints
            so Docker containers inherit CPU pinning via
            OSS_FUZZ_CGROUP_PARENT (default: False)

    Returns:
        Exit code (0 for success)
    """
    from crsbench.utils.cpu_pool import CPUPool, format_cpuset
    from crsbench.utils.size_parser import parse_size_to_bytes

    # Set supervisor environment variable for logger
    os.environ["CRSBENCH_SUPERVISOR"] = "1"
    logger.info("Starting supervisor mode for dynamic CPU allocation...")

    # Create all filestore directories from config if they don't exist
    filestore_vars = [
        "CRSBENCH_WORKER_EXPERIMENT_FILESTORE",
        "CRSBENCH_WORKER_REPORT_FILESTORE",
        "CRSBENCH_WORKER_EXPERIMENT_RESULTS_FILESTORE",
        "CRSBENCH_WORKER_REPORTS_RESULTS_FILESTORE",
    ]
    for var in filestore_vars:
        filestore = os.environ.get(var)
        if filestore:
            # Create base path and experiment-specific subdirectory
            base_path = Path(filestore)
            base_path.mkdir(parents=True, exist_ok=True)
            exp_path = base_path / experiment_name
            exp_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Ensured filestore exists: {exp_path}")

    if use_cpuset:
        # Parse cores: if it looks like an integer, pass as int
        cores_arg: Union[str, int, None] = None
        if cores is not None:
            try:
                cores_arg = int(cores)
            except ValueError:
                cores_arg = cores  # cpuset string
        cpu_pool = CPUPool(cores=cores_arg, skip_cpus=skip_cpus)
    else:
        cpu_pool = None

    # Cgroup initialization (if enabled)
    cgroup_base: Optional[Path] = None
    if use_cgroups:
        from crsbench.utils.cgroup import (
            cleanup_stale_cgroups,
            run_preflight_checks,
            setup_cgroup_hierarchy,
        )

        cgroup_base = run_preflight_checks()  # Raises CgroupError on failure
        setup_cgroup_hierarchy(cgroup_base)
        cleaned = cleanup_stale_cgroups(cgroup_base)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale cgroup(s)")

    workers: dict[
        int, tuple[multiprocessing.Process, list[int], str, int, Optional[Path]]
    ] = {}  # pid -> (process, cpus, job_id, worker_num, cgroup_path)
    used_worker_nums: set[int] = set()  # Track which worker numbers are in use

    # Disk space checking state
    minimum_disk_bytes = parse_size_to_bytes(minimum_disk_size)
    disk_space_ok = True
    last_disk_check = 0.0

    try:
        # Connect to Redis
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        queue_name = queue_name or f"crsbench_{experiment_name}"
        queue = rq.Queue(queue_name, connection=redis_conn)  # type: ignore[attr-defined]

        logger.info(f"Supervisor connected to queue: {queue_name}")
        if cpu_pool:
            logger.info(f"CPU pool initialized with {cpu_pool.total_cpus} CPUs")

        while True:
            # Cleanup finished workers
            for pid in list(workers.keys()):
                proc, cpus, job_id, worker_num, cgroup_path_entry = workers[pid]
                if not proc.is_alive():
                    proc.join()

                    # Enqueue dependents so dependencies resolve immediately
                    from crsbench.distributed.ci_supervisor import (
                        _enqueue_dependents_for_job,
                    )

                    _enqueue_dependents_for_job(redis_conn, job_id)

                    if cpu_pool and cpus:
                        cpu_pool.release(cpus)
                        logger.info(
                            f"Worker (PID: {pid}) finished, released CPUs {cpus}"
                        )
                    if cgroup_path_entry:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path_entry, force=True)
                    # Free up the worker number for reuse
                    used_worker_nums.discard(worker_num)
                    del workers[pid]

            # Check disk space periodically
            current_time = time.time()
            if current_time - last_disk_check >= disk_check_interval:
                # Get filestore path from worker override or use cwd
                filestore_path = Path(
                    os.environ.get("CRSBENCH_WORKER_EXPERIMENT_FILESTORE") or Path.cwd()
                )
                available_bytes = check_disk_space(filestore_path)
                last_disk_check = current_time

                if available_bytes < minimum_disk_bytes:
                    if disk_space_ok:
                        # Transition from OK to low disk space
                        logger.warning(
                            f"Disk space below threshold: {available_bytes / (1024**3):.2f}GB available, "
                            f"minimum required: {minimum_disk_bytes / (1024**3):.2f}GB. "
                            f"Pausing job processing until space is available."
                        )
                        disk_space_ok = False
                elif not disk_space_ok:
                    # Disk space recovered
                    logger.info(
                        f"Disk space recovered: {available_bytes / (1024**3):.2f}GB available. "
                        f"Resuming job processing."
                    )
                    disk_space_ok = True

            # Check if queue has jobs
            queue_count = queue.count
            # Continuous mode: keep running and waiting for new jobs
            # (don't exit when queue is empty)

            # Try to start new worker if jobs available and we have capacity
            # Also check if disk space is sufficient
            if queue_count > 0 and len(workers) < max_workers and disk_space_ok:
                # Properly dequeue job from queue (atomically removes it)
                result = rq.Queue.dequeue_any(
                    [queue],
                    timeout=None,  # Non-blocking check
                    connection=redis_conn,
                )

                if result:
                    job, _ = result

                    # Skip jobs that are already finished or failed (stale queue entries)
                    job_status = job.get_status()
                    if job_status in ("finished", "failed"):
                        logger.debug(
                            f"Skipping stale job {job.id[:30]} (status={job_status})"
                        )
                        continue

                    cpu_count = job.meta.get("cpu_count", 4)

                    # Try to allocate CPUs
                    cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None

                    if cpu_pool is None or cpus is not None:
                        # Update job metadata with allocated CPUs (as cpuset string)
                        cpuset_str = ""
                        if cpus:
                            # Convert CPU list to cpuset string format (e.g., [0,1,2,3] -> "0-3")
                            cpuset_str = format_cpuset(cpus)
                            job.meta["allocated_cpus"] = cpuset_str
                            job.save_meta()

                        # Find lowest available worker number (like true worker pool)
                        worker_num = None
                        for i in range(1, max_workers + 1):
                            if i not in used_worker_nums:
                                worker_num = i
                                break

                        if worker_num is None:
                            # Shouldn't happen since len(workers) < max_workers
                            worker_num = len(workers) + 1

                        used_worker_nums.add(worker_num)
                        name = f"{worker_name}-{worker_num}"

                        # Create cgroup for this worker (if cgroups enabled)
                        cgroup_path: Optional[Path] = None
                        if cgroup_base is not None and cpuset_str:
                            from crsbench.utils.cgroup import (
                                cgroup_path_for_docker,
                                create_cgroup,
                            )

                            cgroup_name = f"worker-{worker_num}"
                            cgroup_path = create_cgroup(
                                cgroup_base,
                                cgroup_name,
                                cpuset=cpuset_str,
                            )
                            cgroup_parent = cgroup_path_for_docker(cgroup_path)
                            job.meta["cgroup_parent"] = cgroup_parent
                            job.save_meta()
                            logger.info(
                                f"Created cgroup {cgroup_name} with cpuset={cpuset_str}"
                            )

                        # Set OSS_FUZZ_CGROUP_PARENT env var so the child process
                        # (and its Docker containers) inherit the constraint.
                        if cgroup_path is not None:
                            from crsbench.utils.cgroup import cgroup_path_for_docker

                            os.environ["OSS_FUZZ_CGROUP_PARENT"] = (
                                cgroup_path_for_docker(cgroup_path)
                            )
                        if cpuset_str:
                            os.environ["OSS_FUZZ_CPUSET_CPUS"] = cpuset_str

                        p = multiprocessing.Process(
                            target=_run_single_job_worker,
                            args=(redis_host, experiment_name, name, job.id),
                            name=f"worker-{worker_num}",
                        )
                        p.start()

                        # Unset after spawning so it doesn't leak to the next worker
                        if cgroup_path is not None:
                            os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                        os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)

                        if p.pid is not None:
                            workers[p.pid] = (
                                p,
                                cpus or [],
                                job.id,
                                worker_num,
                                cgroup_path,
                            )

                        if cpus:
                            logger.info(
                                f"Started worker {name} (PID: {p.pid}) for job {job.id[:8]} with {len(cpus)} CPUs: {cpus}"
                            )
                        else:
                            logger.info(
                                f"Started worker {name} (PID: {p.pid}) for job {job.id[:8]}"
                            )
                    else:
                        # Not enough CPUs - re-enqueue the job at the front
                        # Important: we dequeued it, so must put it back if we can't run it
                        queue.enqueue_job(job, at_front=True)
                        logger.debug(
                            f"Job {job.id[:8]} needs {cpu_count} CPUs, only {cpu_pool.available_count()} available. Re-enqueued for later."
                        )

            # Brief sleep to avoid busy-waiting
            time.sleep(0.5)

        logger.info("Supervisor shutting down")
        return 0

    except KeyboardInterrupt:
        logger.info("\nReceived interrupt signal, terminating workers...")
        for _pid, (p, _cpus, _job_id, _wn, _cg) in workers.items():
            if p.is_alive():
                p.terminate()
        for pid, (p, cpus, _job_id, _wn, cgroup_path_entry) in workers.items():
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing worker (PID: {pid})")
                p.kill()
                p.join()
            if cpu_pool and cpus:
                cpu_pool.release(cpus)
            if cgroup_path_entry:
                from crsbench.utils.cgroup import cleanup_cgroup

                cleanup_cgroup(cgroup_path_entry, force=True)
        return 0
    except Exception as e:
        logger.error(f"Supervisor error: {e}", exc_info=True)
        return 3


def _ci_job_runner(
    redis_host: str,
    child_name: str,
    job_id: str,
) -> None:
    """Adapter for ci_supervisor: delegates to _run_single_job_worker."""
    _run_single_job_worker(redis_host, "", child_name, job_id)


def _run_single_job_worker(
    redis_host: str,
    _experiment_name: str,
    worker_name: str,
    job_id: str,
):
    """Worker that executes a single specific job with proper status tracking.

    CPU affinity is handled by the job itself via allocated_cpus in job metadata.

    Args:
        redis_host: Redis server hostname
        _experiment_name: Experiment identifier (unused, kept for consistency)
        worker_name: Worker name for identification
        job_id: RQ job ID to execute
    """
    from rq.executions import Execution
    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry, FinishedJobRegistry
    from rq.results import Result

    from crsbench.utils.logger import add_file_handler, remove_file_handler

    # Reconfigure logging in subprocess
    configure_logger(level=os.environ.get("LOG_LEVEL", "INFO").upper(), sink=sys.stdout)

    # Set up per-worker logging
    worker_log_handler = None
    experiment_filestore = os.environ.get("CRSBENCH_WORKER_EXPERIMENT_FILESTORE")
    experiment_name_env = os.environ.get("CRSBENCH_EXPERIMENT_NAME")

    if experiment_filestore and experiment_name_env:
        worker_log_dir = (
            Path(experiment_filestore) / experiment_name_env / "worker-logs"
        )
        worker_log_path = worker_log_dir / f"{worker_name}.log"
        worker_log_handler = add_file_handler(
            worker_log_path,
            level="DEBUG",
            rotation="100 MB",
            retention="7 days",
        )
        logger.info(f"Per-worker logging enabled: {worker_log_path}")

    try:
        # Connect to Redis
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        # Store friendly worker name in environment for job metadata
        os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = worker_name

        # Fetch job and get queue
        job = rq.job.Job.fetch(job_id, connection=redis_conn)  # type: ignore[attr-defined]
        queue = rq.Queue(job.origin, connection=redis_conn)  # type: ignore[attr-defined]

        # Get registries for status tracking
        finished_registry = FinishedJobRegistry(queue=queue)
        failed_registry = FailedJobRegistry(queue=queue)

        logger.info(f"Worker {worker_name} executing job {job_id}")

        # Log allocated CPUs if present
        allocated_cpus = job.meta.get("allocated_cpus")
        if allocated_cpus:
            logger.info(f"Job {job_id} assigned CPUs: {allocated_cpus}")

        # Create execution and mark job as STARTED
        execution = None
        with redis_conn.pipeline() as pipeline:
            # Prepare job for execution (sets status to STARTED)
            job.prepare_for_execution(worker_name, pipeline=pipeline)
            # Create execution object (automatically adds to StartedJobRegistry)
            execution = Execution.create(job, ttl=-1, pipeline=pipeline)
            pipeline.execute()

        try:
            # Execute the job
            result = job.perform()

            # Check if this is a TrialResult with success=False
            if isinstance(result, TrialResult) and not result.success:
                # Treat as failed job
                with redis_conn.pipeline() as pipeline:
                    job._status = JobStatus.FAILED
                    job.ended_at = rq.utils.now()  # type: ignore[attr-defined]
                    job._result = result
                    job.save_meta()
                    pipeline.hset(
                        job.key,
                        mapping={
                            "status": JobStatus.FAILED,
                            "ended_at": rq.utils.utcformat(job.ended_at),  # type: ignore[attr-defined]
                        },
                    )
                    # Store trial error as exc_string for failed registry
                    exc_string = f"Trial failed: {result.error_type}: {result.error}"
                    # Store result in Redis (RQ 2.x uses separate result keys)
                    Result.create(
                        job,
                        Result.Type.FAILED,
                        ttl=-1,
                        return_value=result,
                        exc_string=exc_string,
                        pipeline=pipeline,
                    )
                    # Remove from started registry and add to failed registry
                    if execution:
                        execution.delete(job, pipeline=pipeline)
                    failed_registry.add(
                        job, ttl=-1, exc_string=exc_string, pipeline=pipeline
                    )
                    pipeline.execute()

                logger.warning(
                    f"Worker {worker_name} job {job_id} failed: {result.error}"
                )
            else:
                # Mark job as FINISHED and cleanup (original success path)
                with redis_conn.pipeline() as pipeline:
                    job._status = JobStatus.FINISHED
                    job.ended_at = rq.utils.now()  # type: ignore[attr-defined]
                    job._result = result
                    job.save_meta()
                    pipeline.hset(
                        job.key,
                        mapping={
                            "status": JobStatus.FINISHED,
                            "ended_at": rq.utils.utcformat(job.ended_at),  # type: ignore[attr-defined]
                        },
                    )
                    # Store result in Redis (RQ 2.x uses separate result keys)
                    Result.create(
                        job,
                        Result.Type.SUCCESSFUL,
                        ttl=-1,
                        return_value=result,
                        pipeline=pipeline,
                    )
                    # Remove from started registry and add to finished registry
                    if execution:
                        execution.delete(job, pipeline=pipeline)
                    finished_registry.add(job, ttl=-1, pipeline=pipeline)
                    pipeline.execute()

                logger.info(f"Worker {worker_name} finished job {job_id}")

        except Exception as e:
            # Mark job as FAILED and cleanup
            import traceback

            exc_string = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )

            with redis_conn.pipeline() as pipeline:
                job._status = JobStatus.FAILED
                job.ended_at = rq.utils.now()  # type: ignore[attr-defined]
                pipeline.hset(
                    job.key,
                    mapping={
                        "status": JobStatus.FAILED,
                        "ended_at": rq.utils.utcformat(job.ended_at),  # type: ignore[attr-defined]
                    },
                )
                # Store failure in Redis (RQ 2.x uses separate result keys)
                Result.create(
                    job,
                    Result.Type.FAILED,
                    ttl=-1,
                    exc_string=exc_string,
                    pipeline=pipeline,
                )
                # Remove from started registry and add to failed registry
                if execution:
                    execution.delete(job, pipeline=pipeline)
                failed_registry.add(
                    job, ttl=-1, exc_string=exc_string, pipeline=pipeline
                )
                pipeline.execute()

            logger.error(
                f"Worker {worker_name} failed job {job_id}: {e}", exc_info=True
            )
            raise

    except Exception as e:
        logger.error(f"Worker {worker_name} error: {e}", exc_info=True)
        raise
    finally:
        # Clean up per-worker logging
        if worker_log_handler is not None:
            remove_file_handler(worker_log_handler)


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
    try:
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

    except Exception:
        # Re-raise all exceptions to be handled by main()
        raise


def check_disk_space(path: Path) -> int:
    """Check available disk space at given path.

    Args:
        path: Path to check disk space for

    Returns:
        Available disk space in bytes
    """
    # Walk up to find an existing directory (handles case where path doesn't exist yet)
    check_path = path
    while not check_path.exists():
        parent = check_path.parent
        if parent == check_path:
            # Reached root without finding existing dir, fall back to cwd
            check_path = Path.cwd()
            break
        check_path = parent

    stat = shutil.disk_usage(check_path)
    return stat.free


def run_worker_continuous(
    redis_host: str,
    experiment_name: str,
    _timeout: int = 3600,
    worker_name: Optional[str] = None,
    num_workers: int = 1,
    queue_name: Optional[str] = None,
    *,
    use_cpuset: bool = False,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    ci_build_jobs: Optional[int] = None,
    ci_build_cores_per_job: Optional[int] = None,
    ci_verify_jobs: Optional[int] = None,
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

    # Resolve queue name
    queue_name = queue_name or f"crsbench_{experiment_name}"

    # CI dual-queue mode
    if ci_build_jobs is not None:
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        logger.info(f"Starting CI dual-queue supervisor for: {experiment_name}")
        exit_code = run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name="crsbench_ci_build",
            verify_queue_name="crsbench_ci_verify",
            worker_name=worker_name,
            build_jobs=ci_build_jobs,
            build_cores_per_job=ci_build_cores_per_job or 1,
            verify_jobs=ci_verify_jobs or ci_build_jobs,
            job_runner=_ci_job_runner,
            use_cpuset=use_cpuset,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            minimum_disk_size=minimum_disk_size,
            disk_check_interval=disk_check_interval,
        )
        if exit_code != 0:
            raise RuntimeError(f"CI supervisor failed with exit code {exit_code}")
        return

    # Always use supervisor mode for consistent behavior
    if use_cpuset:
        # Use supervisor mode for CPU affinity
        logger.info(
            f"Starting supervisor with {num_workers} workers and CPU affinity for: {experiment_name}"
        )
        exit_code = _run_supervisor(
            redis_host,
            experiment_name,
            worker_name,
            num_workers,
            queue_name=queue_name,
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
