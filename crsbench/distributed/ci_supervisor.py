"""Dual-queue CI supervisor for evaluator.

``crsbench evaluator --ci`` uses this supervisor to dequeue from a *build*
queue and a *verify* queue with independent concurrency limits and per-queue
CPU allocations.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple, Optional, Union

from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.utils.logger import get_logger

if REDIS_AVAILABLE:
    import rq
    import rq.job

if TYPE_CHECKING:
    from redis import Redis

logger = get_logger(__name__)

# job_runner(redis_host, child_name, job_id) -> None
type JobRunner = Callable[[str, str, str], None]


class WorkerEntry(NamedTuple):
    """Metadata for an active child worker process."""

    process: multiprocessing.Process
    cpus: list[int]
    job_id: str
    worker_num: int
    cgroup_path: Optional[Path]


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
    verify_cores_per_job: int = 1,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    continuous: bool = True,
    idle_timeout: int = 0,
) -> int:
    """Dual-queue supervisor for evaluator CI mode.

    Dequeues from *build_queue_name* (priority) and *verify_queue_name*,
    enforcing separate concurrency limits and CPU-per-job allocations.

    Args:
        redis_host: Redis server hostname.
        build_queue_name: Redis queue for build jobs.
        verify_queue_name: Redis queue for verify jobs.
        worker_name: Base worker name for child processes.
        build_jobs: Max concurrent build jobs.
        build_cores_per_job: CPUs allocated per build job.
        verify_cores_per_job: CPUs allocated per verify job.
        verify_jobs: Max concurrent verify jobs.
        job_runner: Callable ``(redis_host, child_name, job_id) -> None``
            spawned in a child process to execute a single job.
        use_cpuset: Enable CPU affinity via CPUPool.
        use_cgroups: Create per-job cgroups with cpuset constraints.
        cores: CPU cores for the pool (integer count or cpuset string).
        skip_cpus: CPUs to exclude (cpuset format).
        minimum_disk_size: Minimum free disk space before pausing.
        disk_check_interval: Seconds between disk space checks.
        idle_timeout: Seconds to wait idle after build phase completes
            before exiting. 0 means disabled (wait forever).

    Returns:
        Exit code (0 for success).
    """
    from crsbench.utils.cpu_pool import CPUPool, format_cpuset
    from crsbench.utils.size_parser import parse_size_to_bytes

    os.environ["CRSBENCH_SUPERVISOR"] = "1"
    logger.info("Starting CI dual-queue supervisor...")
    logger.info(
        f"Build concurrency: {build_jobs} jobs x {build_cores_per_job} CPUs each"
    )
    logger.info(
        f"Verify concurrency: {verify_jobs} jobs x {verify_cores_per_job} CPUs each"
    )

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
    build_active: dict[int, WorkerEntry] = {}
    verify_active: dict[int, WorkerEntry] = {}
    # Cgroup paths that failed immediate removal (EBUSY from Docker shim)
    deferred_cgroup_cleanup: list[Path] = []

    used_worker_nums: set[int] = set()
    max_total = build_jobs + verify_jobs
    build_phase_complete = False
    idle_since: float = 0.0

    # Disk space state
    minimum_disk_bytes = parse_size_to_bytes(minimum_disk_size)
    disk_space_ok = True
    last_disk_check = 0.0

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

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
                build_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )
            _reap_finished(
                verify_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )

            # --- Sweep deferred cgroup removals (non-blocking) ---
            if deferred_cgroup_cleanup:
                _sweep_deferred_cgroups(deferred_cgroup_cleanup)

            # --- Detect build phase completion ---
            if not build_phase_complete and not build_active and build_queue.count == 0:
                build_phase_complete = True
                idle_since = time.time()
                logger.info(
                    "=" * 60
                    + "\n  BUILD PHASE COMPLETE — all build jobs finished"
                    + "\n  Switching to verify-only mode"
                    + f"\n  Verify queue: {verify_queue.count} pending"
                    + "\n"
                    + "=" * 60
                )

            # --- Idle timeout check (continuous mode only) ---
            if continuous and build_phase_complete and idle_timeout > 0:
                if not verify_active and verify_queue.count == 0:
                    elapsed = time.time() - idle_since
                    if elapsed >= idle_timeout:
                        logger.info(
                            f"Idle timeout reached ({idle_timeout}s) — "
                            "no verify jobs after build phase. Exiting."
                        )
                        break
                else:
                    # Reset idle timer when there's work
                    idle_since = time.time()

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

            # --- Exit when all work is done (non-continuous mode only) ---
            # Check cheapest condition first (local dict) then LLEN then ZCARD
            if not continuous:
                no_active_workers = not build_active and not verify_active
                if no_active_workers:
                    no_queued = build_queue.count == 0 and verify_queue.count == 0
                    if no_queued:
                        no_deferred = (
                            build_queue.deferred_job_registry.count == 0
                            and verify_queue.deferred_job_registry.count == 0
                        )
                        if no_deferred:
                            logger.info(
                                "All queues empty and no active workers — exiting"
                            )
                            break

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

            # Skip jobs that are already finished or failed (stale queue entries)
            job_status = job.get_status()
            if job_status in ("finished", "failed"):
                logger.debug(f"Skipping stale job {job.id[:30]} (status={job_status})")
                continue

            is_build = queue_obj.name == build_queue_name
            queue_label = "build" if is_build else "verify"
            cpu_count = build_cores_per_job if is_build else verify_cores_per_job

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

            # Cgroup + spawn — wrapped so a stale root-owned cgroup
            # doesn't crash the entire supervisor (re-enqueue and retry).
            try:
                cgroup_path: Optional[Path] = None
                if cgroup_base is not None and cpuset_str:
                    from crsbench.utils.cgroup import (
                        cgroup_path_for_docker,
                        create_cgroup,
                    )

                    cgroup_name = f"{queue_label}-{worker_num}"
                    cgroup_path = create_cgroup(
                        cgroup_base, cgroup_name, cpuset=cpuset_str
                    )
                    cgroup_parent = cgroup_path_for_docker(cgroup_path)
                    job.meta["cgroup_parent"] = cgroup_parent
                    job.save_meta()
                    logger.info(f"Created cgroup {cgroup_name} cpuset={cpuset_str}")

                if cgroup_path is not None:
                    os.environ["OSS_FUZZ_CGROUP_PARENT"] = cgroup_path_for_docker(
                        cgroup_path
                    )
                if cpuset_str:
                    os.environ["OSS_FUZZ_CPUSET_CPUS"] = cpuset_str

                # Spawn child process
                p = multiprocessing.Process(
                    target=job_runner,
                    args=(redis_host, child_name, job.id),
                    name=f"ci-{queue_label}-{worker_num}",
                )
                p.start()

                if cgroup_path is not None:
                    os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)

                if p.pid is not None:
                    entry = WorkerEntry(p, cpus or [], job.id, worker_num, cgroup_path)
                    if is_build:
                        build_active[p.pid] = entry
                    else:
                        verify_active[p.pid] = entry

                logger.info(
                    f"Started {queue_label} job {job.id[:8]} (PID: {p.pid})"
                    + (f" with {len(cpus)} CPUs: {cpuset_str}" if cpus else "")
                )
            except OSError as spawn_err:
                # Cgroup permission error (e.g. stale root-owned cgroup from
                # a previous Docker run). Release resources and re-enqueue.
                logger.warning(
                    f"Failed to spawn worker {worker_num}: {spawn_err}. "
                    "Re-enqueuing job."
                )
                # Clean up env vars that may have been set before the error
                os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)
                # Keep worker_num in used_worker_nums so it's skipped on
                # retry (the stale cgroup won't go away by itself).
                if cpu_pool and cpus:
                    cpu_pool.release(cpus)
                queue_obj.enqueue_job(job, at_front=True)
                time.sleep(0.5)
                continue

    except KeyboardInterrupt:
        logger.info("\nCI supervisor interrupted, terminating workers...")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        return 0
    except Exception as e:
        logger.error(f"CI supervisor error: {e}", exc_info=True)
        return 3

    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enqueue_dependents_for_job(redis_conn: Redis, job_id: str) -> None:
    """Enqueue dependent jobs after a job finishes.

    Uses RQ's ``Queue.enqueue_dependents()`` to move jobs from the
    deferred registry to their target queues.  Works cross-queue:
    a build job finishing can trigger verify jobs on a different queue.
    """
    try:
        job = rq.job.Job.fetch(job_id, connection=redis_conn)
        if job.get_status() != "finished":
            return
        queue = rq.Queue(job.origin, connection=redis_conn)
        queue.enqueue_dependents(job)
    except Exception:
        logger.debug(f"Failed to enqueue dependents for {job_id}", exc_info=True)


def _try_remove_cgroup(cgroup_path: Path) -> bool:
    """Try once to remove a cgroup directory without blocking.

    Called after our worker has finished, so the cgroup should be empty.
    Removes leftover Docker child cgroup directories and retries.

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
            from crsbench.utils.cgroup import _remove_cgroup_children

            _remove_cgroup_children(cgroup_path)
            try:
                cgroup_path.rmdir()
                return True
            except OSError:
                logger.debug(f"Cgroup {cgroup_path.name} still busy, deferring")
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
    active: dict[int, WorkerEntry],
    cpu_pool: Optional[object],
    used_worker_nums: set[int],
    deferred_cgroup_cleanup: list[Path],
    redis_conn: Optional[Redis] = None,
) -> None:
    """Clean up finished child processes and release their resources.

    After a child completes, enqueues any dependent jobs via RQ's
    ``Queue.enqueue_dependents()`` so cross-queue dependencies resolve
    immediately rather than waiting for periodic recovery sweeps.

    Cgroup removal is attempted once without retries to avoid blocking
    the supervisor loop.  If the cgroup is still busy (Docker shim
    processes haven't exited yet), the path is deferred for later sweep.
    """
    for pid in list(active.keys()):
        entry = active[pid]
        if not entry.process.is_alive():
            entry.process.join()

            # Enqueue dependents so cross-queue dependencies resolve immediately
            if redis_conn is not None:
                _enqueue_dependents_for_job(redis_conn, entry.job_id)

            if cpu_pool and entry.cpus:
                cpu_pool.release(entry.cpus)  # type: ignore[union-attr]
                logger.info(f"Worker (PID: {pid}) finished, released CPUs {entry.cpus}")
            if entry.cgroup_path:
                if not _try_remove_cgroup(entry.cgroup_path):
                    deferred_cgroup_cleanup.append(entry.cgroup_path)
            used_worker_nums.discard(entry.worker_num)
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
    active: dict[int, WorkerEntry],
    cpu_pool: Optional[object],
) -> None:
    """Terminate and clean up all active workers."""
    for entry in active.values():
        if entry.process.is_alive():
            entry.process.terminate()
    for pid, entry in active.items():
        entry.process.join(timeout=5)
        if entry.process.is_alive():
            logger.warning(f"Force killing worker (PID: {pid})")
            entry.process.kill()
            entry.process.join()
        if cpu_pool and entry.cpus:
            cpu_pool.release(entry.cpus)  # type: ignore[union-attr]
        if entry.cgroup_path:
            from crsbench.utils.cgroup import cleanup_cgroup

            cleanup_cgroup(entry.cgroup_path, force=True)
