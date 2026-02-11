"""Shared dual-queue CI supervisor for worker and evaluator.

Both ``crsbench worker --queue ci`` and ``crsbench evaluator`` share the same
core loop: dequeue from a *build* queue and a *verify* queue with independent
concurrency limits and per-queue CPU allocations.

This module extracts that common loop so both commands reuse it.
"""

import multiprocessing
import os
import time
from pathlib import Path
from typing import Callable, Optional, Union

from crsbench.utils.logger import get_logger

try:
    import redis
    import rq

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = get_logger(__name__)

# job_runner(redis_host, child_name, job_id) -> None
type JobRunner = Callable[[str, str, str], None]


def run_ci_supervisor(
    redis_host: str,
    build_queue_name: str,
    verify_queue_name: str,
    worker_name: str,
    build_jobs: int,
    build_cores_per_job: int,
    verify_jobs: int,
    job_runner: JobRunner,
    *,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
) -> int:
    """Dual-queue supervisor shared by worker and evaluator.

    Dequeues from *build_queue_name* (priority) and *verify_queue_name*,
    enforcing separate concurrency limits and CPU-per-job allocations.

    Args:
        redis_host: Redis server hostname.
        build_queue_name: Redis queue for build jobs.
        verify_queue_name: Redis queue for verify jobs.
        worker_name: Base worker name for child processes.
        build_jobs: Max concurrent build jobs.
        build_cores_per_job: CPUs allocated per build job.
        verify_jobs: Max concurrent verify jobs (1 CPU each).
        job_runner: Callable ``(redis_host, child_name, job_id) -> None``
            spawned in a child process to execute a single job.
        use_cpuset: Enable CPU affinity via CPUPool.
        use_cgroups: Create per-job cgroups with cpuset constraints.
        cores: CPU cores for the pool (integer count or cpuset string).
        skip_cpus: CPUs to exclude (cpuset format).
        minimum_disk_size: Minimum free disk space before pausing.
        disk_check_interval: Seconds between disk space checks.

    Returns:
        Exit code (0 for success).
    """
    from crsbench.distributed.worker import check_disk_space
    from crsbench.utils.cpu_pool import CPUPool, format_cpuset
    from crsbench.utils.size_parser import parse_size_to_bytes

    os.environ["CRSBENCH_SUPERVISOR"] = "1"
    logger.info("Starting CI dual-queue supervisor...")
    logger.info(
        f"Build concurrency: {build_jobs} jobs x {build_cores_per_job} CPUs each"
    )
    logger.info(f"Verify concurrency: {verify_jobs} jobs x 1 CPU each")

    # CPU pool setup
    cpu_pool: Optional[CPUPool] = None
    if use_cpuset:
        cores_arg: Union[str, int, None] = None
        if cores is not None:
            try:
                cores_arg = int(cores)
            except ValueError:
                cores_arg = cores
        cpu_pool = CPUPool(cores=cores_arg, skip_cpus=skip_cpus)

    # Cgroup initialization
    cgroup_base: Optional[Path] = None
    if use_cgroups:
        from crsbench.utils.cgroup import (
            cleanup_stale_cgroups,
            run_preflight_checks,
            setup_cgroup_hierarchy,
        )

        cgroup_base = run_preflight_checks()
        setup_cgroup_hierarchy(cgroup_base)
        cleaned = cleanup_stale_cgroups(cgroup_base)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale cgroup(s)")

    # Separate tracking for build vs verify workers.
    # pid -> (process, cpus, job_id, worker_num, cgroup_path)
    build_active: dict[
        int, tuple[multiprocessing.Process, list[int], str, int, Optional[Path]]
    ] = {}
    verify_active: dict[
        int, tuple[multiprocessing.Process, list[int], str, int, Optional[Path]]
    ] = {}
    # Cgroup paths that failed immediate removal (EBUSY from Docker shim)
    deferred_cgroup_cleanup: list[Path] = []

    used_worker_nums: set[int] = set()
    max_total = build_jobs + verify_jobs
    build_phase_complete = False

    # Disk space state
    minimum_disk_bytes = parse_size_to_bytes(minimum_disk_size)
    disk_space_ok = True
    last_disk_check = 0.0

    try:
        redis_password = os.environ.get("REDIS_PASSWORD") or None
        redis_conn = redis.Redis(
            host=redis_host,
            password=redis_password,
            socket_connect_timeout=5,
        )
        redis_conn.ping()

        build_queue = rq.Queue(build_queue_name, connection=redis_conn)
        verify_queue = rq.Queue(verify_queue_name, connection=redis_conn)

        logger.info(
            f"CI supervisor connected: {build_queue_name} (build), "
            f"{verify_queue_name} (verify)"
        )
        if cpu_pool:
            logger.info(f"CPU pool: {cpu_pool.total_cpus} CPUs")

        while True:
            # --- Cleanup finished workers ---
            _reap_finished(
                build_active, cpu_pool, used_worker_nums, deferred_cgroup_cleanup
            )
            _reap_finished(
                verify_active, cpu_pool, used_worker_nums, deferred_cgroup_cleanup
            )

            # --- Sweep deferred cgroup removals (non-blocking) ---
            if deferred_cgroup_cleanup:
                _sweep_deferred_cgroups(deferred_cgroup_cleanup)

            # --- Detect build phase completion ---
            if not build_phase_complete and not build_active and build_queue.count == 0:
                build_phase_complete = True
                logger.info(
                    "=" * 60
                    + "\n  BUILD PHASE COMPLETE — all build jobs finished"
                    + "\n  Switching to verify-only mode"
                    + f"\n  Verify queue: {verify_queue.count} pending"
                    + "\n"
                    + "=" * 60
                )

            # --- Disk space check ---
            current_time = time.time()
            if current_time - last_disk_check >= disk_check_interval:
                filestore_path = Path(
                    os.environ.get("CRSBENCH_WORKER_EXPERIMENT_FILESTORE")
                    or str(Path.cwd())
                )
                available_bytes = check_disk_space(filestore_path)
                last_disk_check = current_time

                if available_bytes < minimum_disk_bytes:
                    if disk_space_ok:
                        logger.warning(
                            f"Disk space low: {available_bytes / (1024**3):.2f}GB "
                            f"(min {minimum_disk_bytes / (1024**3):.2f}GB). Pausing."
                        )
                        disk_space_ok = False
                elif not disk_space_ok:
                    logger.info(
                        f"Disk space recovered: "
                        f"{available_bytes / (1024**3):.2f}GB. Resuming."
                    )
                    disk_space_ok = True

            if not disk_space_ok:
                time.sleep(0.5)
                continue

            # --- Determine which queues have capacity ---
            queues_with_capacity: list[rq.Queue] = []
            if len(build_active) < build_jobs and build_queue.count > 0:
                queues_with_capacity.append(build_queue)
            if len(verify_active) < verify_jobs and verify_queue.count > 0:
                queues_with_capacity.append(verify_queue)

            if not queues_with_capacity:
                time.sleep(0.5)
                continue

            # Dequeue with build priority (build queue first in list)
            result = rq.Queue.dequeue_any(
                queues_with_capacity,
                timeout=None,
                connection=redis_conn,
            )

            if not result:
                time.sleep(0.5)
                continue

            job, queue_obj = result
            is_build = queue_obj.name == build_queue_name
            queue_label = "build" if is_build else "verify"
            cpu_count = build_cores_per_job if is_build else 1

            # Allocate CPUs
            cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None

            if cpu_pool is not None and cpus is None:
                queue_obj.enqueue_job(job, at_front=True)
                logger.debug(
                    f"Job {job.id[:8]} needs {cpu_count} CPUs, "
                    f"only {cpu_pool.available_count()} available. Re-enqueued."
                )
                time.sleep(0.5)
                continue

            # Set up metadata
            cpuset_str = ""
            if cpus:
                cpuset_str = format_cpuset(cpus)
                job.meta["allocated_cpus"] = cpuset_str
                job.save_meta()

            # Worker numbering
            worker_num = _next_worker_num(used_worker_nums, max_total)
            used_worker_nums.add(worker_num)
            child_name = f"{worker_name}-{worker_num}"

            # Cgroup
            cgroup_path: Optional[Path] = None
            if cgroup_base is not None and cpuset_str:
                from crsbench.utils.cgroup import (
                    cgroup_path_for_docker,
                    create_cgroup,
                )

                cgroup_name = f"{queue_label}-{worker_num}"
                cgroup_path = create_cgroup(cgroup_base, cgroup_name, cpuset=cpuset_str)
                cgroup_parent = cgroup_path_for_docker(cgroup_path)
                job.meta["cgroup_parent"] = cgroup_parent
                job.save_meta()
                logger.info(f"Created cgroup {cgroup_name} cpuset={cpuset_str}")

            if cgroup_path is not None:
                from crsbench.utils.cgroup import cgroup_path_for_docker

                os.environ["OSS_FUZZ_CGROUP_PARENT"] = cgroup_path_for_docker(
                    cgroup_path
                )

            # Spawn child process
            p = multiprocessing.Process(
                target=job_runner,
                args=(redis_host, child_name, job.id),
                name=f"ci-{queue_label}-{worker_num}",
            )
            p.start()

            if cgroup_path is not None:
                os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)

            if p.pid is not None:
                entry = (p, cpus or [], job.id, worker_num, cgroup_path)
                if is_build:
                    build_active[p.pid] = entry
                else:
                    verify_active[p.pid] = entry

            logger.info(
                f"Started {queue_label} job {job.id[:8]} (PID: {p.pid})"
                + (f" with {len(cpus)} CPUs: {cpuset_str}" if cpus else "")
            )

    except KeyboardInterrupt:
        logger.info("\nCI supervisor interrupted, terminating workers...")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        return 0
    except Exception as e:
        logger.error(f"CI supervisor error: {e}", exc_info=True)
        return 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_remove_cgroup(cgroup_path: Path) -> bool:
    """Try once to remove a cgroup directory without blocking.

    Returns True if removed (or absent), False if still busy.
    """
    import errno

    if not cgroup_path.exists():
        return True
    try:
        cgroup_path.rmdir()
        return True
    except OSError as e:
        if e.errno == errno.EBUSY:
            logger.debug(f"Cgroup {cgroup_path.name} busy, deferring cleanup")
            return False
        logger.error(f"Cgroup cleanup failed for {cgroup_path}: {e}")
        return False


def _next_worker_num(used: set[int], max_total: int) -> int:
    """Find the lowest available worker number."""
    for i in range(1, max_total + 1):
        if i not in used:
            return i
    return max_total + 1


def _reap_finished(
    active: dict[
        int,
        tuple[multiprocessing.Process, list[int], str, int, Optional[Path]],
    ],
    cpu_pool: Optional[object],
    used_worker_nums: set[int],
    deferred_cgroup_cleanup: list[Path],
) -> None:
    """Clean up finished child processes and release their resources.

    Cgroup removal is attempted once without retries to avoid blocking
    the supervisor loop.  If the cgroup is still busy (Docker shim
    processes haven't exited yet), the path is deferred for later sweep.
    """
    for pid in list(active.keys()):
        proc, cpus, _job_id, worker_num, cgroup_path_entry = active[pid]
        if not proc.is_alive():
            proc.join()
            if cpu_pool and cpus:
                cpu_pool.release(cpus)  # type: ignore[union-attr]
                logger.info(f"Worker (PID: {pid}) finished, released CPUs {cpus}")
            if cgroup_path_entry:
                if not _try_remove_cgroup(cgroup_path_entry):
                    deferred_cgroup_cleanup.append(cgroup_path_entry)
            used_worker_nums.discard(worker_num)
            del active[pid]


def _sweep_deferred_cgroups(deferred: list[Path]) -> None:
    """Try to remove previously deferred cgroup paths (single silent attempt)."""
    remaining: list[Path] = []
    for cg_path in deferred:
        if not _try_remove_cgroup(cg_path):
            remaining.append(cg_path)
    deferred.clear()
    deferred.extend(remaining)


def _terminate_all(
    active: dict[
        int,
        tuple[multiprocessing.Process, list[int], str, int, Optional[Path]],
    ],
    cpu_pool: Optional[object],
) -> None:
    """Terminate and clean up all active workers."""
    for _pid, (p, _cpus, _job_id, _wn, _cg) in active.items():
        if p.is_alive():
            p.terminate()
    for pid, (p, cpus, _job_id, _wn, cgroup_path_entry) in active.items():
        p.join(timeout=5)
        if p.is_alive():
            logger.warning(f"Force killing worker (PID: {pid})")
            p.kill()
            p.join()
        if cpu_pool and cpus:
            cpu_pool.release(cpus)  # type: ignore[union-attr]
        if cgroup_path_entry:
            from crsbench.utils.cgroup import cleanup_cgroup

            cleanup_cgroup(cgroup_path_entry)
