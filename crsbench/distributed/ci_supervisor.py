"""Dual-queue CI supervisor for evaluator.

``crsbench evaluator --ci`` uses this supervisor to dequeue from a *build*
queue and a *verify* queue with independent concurrency limits and per-queue
CPU allocations.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.context
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple, Optional, Union

from crsbench.distributed.common import normalize_cpu_tag
from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    FairSchedulerState,
    QueuedCandidate,
    build_scheduler_owner_key_from_payload,
    choose_next_fair_candidate,
    choose_next_queue_class,
    clone_fair_scheduler_state,
    restore_fair_scheduler_state,
)
from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.utils.cpu_pool import auto_cores_per_job, visible_cpu_count
from crsbench.utils.logger import get_logger

if REDIS_AVAILABLE:
    import redis.exceptions
    import rq
    import rq.job
    from rq.exceptions import NoSuchJobError
    from rq.job import JobStatus

if TYPE_CHECKING:
    from redis import Redis

logger = get_logger(__name__)
DEQUEUE_POLL_TIMEOUT_SECONDS = 1
NON_CONTINUOUS_CPU_MISMATCH_LIMIT = 3
MAX_CONSECUTIVE_DEQUEUE_ERRORS = 10
FAIR_CLAIM_LEASE_TTL_SECONDS = 120

# Use 'fork' context so child processes inherit module-level state
# (e.g. _verification_engine, _benchmarks_root) from the parent.
# Python 3.14 defaults to 'forkserver' which does NOT inherit these.
_mp_ctx: multiprocessing.context.ForkContext = multiprocessing.get_context("fork")  # type: ignore[assignment]
CPU_TAG_MISMATCH_EXIT_CODE = 4

# job_runner(redis_host, child_name, job_id) -> None
type JobRunner = Callable[[str, str, str], None]
type QueueRefresher = Callable[["Redis"], tuple[list[str], list[str]]]


class WorkerEntry(NamedTuple):
    """Metadata for an active child worker process."""

    process: multiprocessing.process.BaseProcess
    cpus: list[int]
    job_id: str
    worker_num: int
    cgroup_path: Optional[Path]
    claim_lease_key: Optional[str]


_CLAIM_FAIR_JOB_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[1])
    -- Do not delete the lease on a failed claim attempt: another evaluator
    -- may have won the race and already owns the live handoff lease.
    redis.call('SET', KEYS[3], ARGV[2], 'EX', tonumber(ARGV[3]))
end
return removed
"""

_MOVE_JOB_ID_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[1])
end
redis.call('DEL', KEYS[3])
return removed
"""

_REMOVE_JOB_ID_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
redis.call('DEL', KEYS[2])
return removed
"""


def _extract_job_payload(job: "rq.job.Job") -> dict[str, object]:
    """Best-effort extraction of the serialized payload carried by an RQ job."""
    raw_args = getattr(job, "args", None)
    raw_kwargs = getattr(job, "kwargs", None)
    if isinstance(raw_args, dict):
        return raw_args
    if (
        isinstance(raw_args, (list, tuple))
        and raw_args
        and isinstance(raw_args[0], dict)
    ):
        return raw_args[0]
    if isinstance(raw_kwargs, dict):
        return raw_kwargs
    return {}


def _scheduler_owner_key_for_job(job: "rq.job.Job", *, queue_name: str) -> str:
    """Return the scheduler owner key for a queued evaluator job."""
    meta_owner = job.meta.get(SCHEDULER_OWNER_KEY_META)
    if isinstance(meta_owner, str) and meta_owner:
        return meta_owner
    return build_scheduler_owner_key_from_payload(
        _extract_job_payload(job),
        fallback_job_id=job.id,
        queue_name=queue_name,
    )


def _claim_lease_key(queue: "rq.Queue", job_id: str) -> str:
    return f"{queue.intermediate_queue.key}:fair-claim:{job_id}"


def _move_job_id(
    redis_conn: "Redis",
    *,
    source_key: str,
    dest_key: str,
    lease_key: str,
    job_id: str,
) -> bool:
    moved = redis_conn.eval(
        _MOVE_JOB_ID_LUA,
        3,
        source_key,
        dest_key,
        lease_key,
        job_id,
    )
    return moved == 1


def _remove_job_id(
    redis_conn: "Redis",
    *,
    list_key: str,
    lease_key: str,
    job_id: str,
) -> bool:
    removed = redis_conn.eval(
        _REMOVE_JOB_ID_LUA,
        2,
        list_key,
        lease_key,
        job_id,
    )
    return removed == 1


def _remove_stale_queue_job_id(queue: "rq.Queue", job_id: str) -> None:
    removed = queue.connection.lrem(queue.key, 1, job_id)
    if removed:
        logger.warning(
            f"Removed stale queue entry {job_id} from {queue.name}; backing RQ job "
            "record was missing."
        )


def _restore_claimed_job(
    redis_conn: "Redis",
    *,
    queue: "rq.Queue",
    job_id: str,
) -> bool:
    try:
        return _move_job_id(
            redis_conn,
            source_key=queue.intermediate_queue.key,
            dest_key=queue.key,
            lease_key=_claim_lease_key(queue, job_id),
            job_id=job_id,
        )
    except Exception as exc:
        logger.warning(
            f"Failed to restore claimed job {job_id} from intermediate queue "
            f"{queue.name}: {exc}"
        )
        return False


def _discard_claimed_job(
    redis_conn: "Redis",
    *,
    queue: "rq.Queue",
    job_id: str,
) -> bool:
    try:
        return _remove_job_id(
            redis_conn,
            list_key=queue.intermediate_queue.key,
            lease_key=_claim_lease_key(queue, job_id),
            job_id=job_id,
        )
    except Exception as exc:
        logger.warning(
            f"Failed to discard claimed job {job_id} from intermediate queue "
            f"{queue.name}: {exc}"
        )
        return False


def _queue_candidates(
    queues: list["rq.Queue"],
    *,
    worker_cpu_tag: Optional[str] = None,
) -> list[list[QueuedCandidate]]:
    """Project queued RQ jobs into fair-selection candidates per queue."""
    if not queues:
        return []

    queue_job_ids = [list(queue.get_job_ids()) for queue in queues]
    all_job_ids = [job_id for job_ids in queue_job_ids for job_id in job_ids]
    if not all_job_ids:
        return [[] for _ in queues]

    jobs = rq.job.Job.fetch_many(all_job_ids, connection=queues[0].connection)
    jobs_by_id = {job.id: job for job in jobs if job is not None}

    queue_candidates: list[list[QueuedCandidate]] = []
    for queue, job_ids in zip(queues, queue_job_ids, strict=False):
        candidates: list[QueuedCandidate] = []
        for job_id in job_ids:
            job = jobs_by_id.get(job_id)
            if job is None:
                _remove_stale_queue_job_id(queue, job_id)
                continue
            if not _matches_cpu_tag(job, worker_cpu_tag):
                continue
            candidates.append(
                QueuedCandidate(
                    queue_name=queue.name,
                    job_id=job_id,
                    owner_key=_scheduler_owner_key_for_job(job, queue_name=queue.name),
                )
            )
        queue_candidates.append(candidates)
    return queue_candidates


def _claim_fair_candidate(
    redis_conn: "Redis",
    *,
    queue_lookup: dict[str, "rq.Queue"],
    candidate: QueuedCandidate,
) -> tuple["rq.job.Job", "rq.Queue"] | None:
    """Atomically move and fetch a selected fair candidate via RQ intermediate state."""
    queue = queue_lookup[candidate.queue_name]
    claimed = redis_conn.eval(
        _CLAIM_FAIR_JOB_LUA,
        3,
        queue.key,
        queue.intermediate_queue.key,
        _claim_lease_key(queue, candidate.job_id),
        candidate.job_id,
        "claimed",
        str(FAIR_CLAIM_LEASE_TTL_SECONDS),
    )
    if not claimed:
        return None
    try:
        job = rq.job.Job.fetch(candidate.job_id, connection=redis_conn)
    except NoSuchJobError:
        _discard_claimed_job(redis_conn, queue=queue, job_id=candidate.job_id)
        logger.debug(f"Fair claim fetched missing job {candidate.job_id}")
        return None
    except Exception:
        restored = _restore_claimed_job(
            redis_conn,
            queue=queue,
            job_id=candidate.job_id,
        )
        if restored:
            logger.warning(
                f"Fair claim fetch failed after moving {candidate.job_id}; "
                "restored job id to queue front."
            )
        else:
            logger.warning(
                f"Fair claim fetch failed after moving {candidate.job_id}; "
                "job remains recoverable in the intermediate queue."
            )
        return None
    return job, queue


def _select_fair_job(
    *,
    redis_conn: "Redis",
    build_queues: list["rq.Queue"],
    verify_queues: list["rq.Queue"],
    build_has_capacity: bool,
    verify_has_capacity: bool,
    state: FairSchedulerState,
    worker_cpu_tag: Optional[str] = None,
) -> tuple["rq.job.Job", "rq.Queue"] | None:
    """Choose and claim the next fair runnable evaluator job."""
    state_snapshot = clone_fair_scheduler_state(state)
    try:
        build_candidates = (
            _queue_candidates(build_queues, worker_cpu_tag=worker_cpu_tag)
            if build_has_capacity
            else []
        )
        verify_candidates = (
            _queue_candidates(verify_queues, worker_cpu_tag=worker_cpu_tag)
            if verify_has_capacity
            else []
        )

        queue_class = choose_next_queue_class(
            build_has_capacity=build_has_capacity,
            verify_has_capacity=verify_has_capacity,
            build_has_work=any(build_candidates),
            verify_has_work=any(verify_candidates),
            state=state,
        )
        if queue_class is None:
            restore_fair_scheduler_state(state, state_snapshot)
            return None

        candidates = build_candidates if queue_class == "build" else verify_candidates
        candidate = choose_next_fair_candidate(
            candidates,
            queue_class=queue_class,
            state=state,
        )
        if candidate is None:
            restore_fair_scheduler_state(state, state_snapshot)
            return None

        queue_lookup = {queue.name: queue for queue in (*build_queues, *verify_queues)}
        result = _claim_fair_candidate(
            redis_conn,
            queue_lookup=queue_lookup,
            candidate=candidate,
        )
        if result is None:
            restore_fair_scheduler_state(state, state_snapshot)
        return result
    except Exception:
        restore_fair_scheduler_state(state, state_snapshot)
        raise


def _safe_cwd() -> Path:
    """Return current working directory, falling back to root on ENOENT."""
    try:
        return Path.cwd()
    except OSError:
        return Path("/")


def _make_unique_cgroup_name(queue_label: str, worker_num: int, job_id: str) -> str:
    """Return a unique cgroup name to avoid stale-name collisions.

    Reusing short names like ``verify-4`` can collide with stale cgroups from a
    previous interrupted run. Include timestamp + job suffix so each spawn gets
    a fresh cgroup path.
    """
    job_suffix = "".join(c if c.isalnum() else "-" for c in job_id)[:24].strip("-")
    if not job_suffix:
        job_suffix = "job"
    return f"{queue_label}-{worker_num}-{int(time.time() * 1000)}-{job_suffix}"


def check_disk_space(path: Path) -> int:
    """Check available disk space at given path.

    Args:
        path: Path to check disk space for

    Returns:
        Available disk space in bytes
    """
    # Walk up to find an existing directory (handles case where path doesn't exist yet)
    check_path = path if path.is_absolute() else (_safe_cwd() / path)
    while not check_path.exists():
        parent = check_path.parent
        if parent == check_path:
            # Reached root without finding existing dir; always check filesystem root
            check_path = Path("/")
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
    build_cores_per_job: Optional[int],
    verify_jobs: int,
    job_runner: JobRunner,
    *,
    verify_cores_per_job: Optional[int] = None,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    continuous: bool = True,
    idle_timeout: int = 0,
    cpu_tag: Optional[str] = None,
    progress_log_every_jobs: int = 0,
) -> int:
    """Dual-queue supervisor for evaluator CI mode.

    Selects runnable work from *build_queue_name* and *verify_queue_name*
    using the evaluator fair scheduler while enforcing separate concurrency
    limits and CPU-per-job allocations.

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
        cpu_tag: Optional worker capability tag. Jobs with mismatched
            ``job.meta['cpu_tag']`` are re-queued.
        progress_log_every_jobs: Emit a handled-job progress log whenever
            this many completed jobs have been reached. ``0`` disables the
            progress log.

    Returns:
        Exit code (0 for success).
    """
    from crsbench.utils.cpu_pool import CPUPool, format_cpuset
    from crsbench.utils.size_parser import parse_size_to_bytes

    os.environ["CRSBENCH_SUPERVISOR"] = "1"
    resolved_build_cores_per_job = (
        build_cores_per_job
        if build_cores_per_job is not None
        else (
            auto_cores_per_job(build_jobs, cores=cores, skip_cpus=skip_cpus)
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )
    resolved_verify_cores_per_job = (
        verify_cores_per_job
        if verify_cores_per_job is not None
        else (
            auto_cores_per_job(max(verify_jobs, 1), cores=cores, skip_cpus=skip_cpus)
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )

    logger.info("Starting CI dual-queue supervisor...")
    logger.info(
        f"Build concurrency: {build_jobs} jobs x {resolved_build_cores_per_job} CPUs each"
    )
    logger.info(
        f"Verify concurrency: {verify_jobs} jobs x {resolved_verify_cores_per_job} CPUs each"
    )
    if cpu_tag:
        logger.info(f"CPU tag filter enabled: {cpu_tag}")

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
    consecutive_cpu_mismatch_requeues = 0
    consecutive_dequeue_errors = 0
    mismatch_job_ids: set[str] = set()
    mismatch_queue_names: set[str] = set()
    cpu_tag_livelock_detected = False
    handled_build_jobs = 0
    handled_verify_jobs = 0
    fair_state = FairSchedulerState()

    # Disk space state
    minimum_disk_bytes = parse_size_to_bytes(minimum_disk_size)
    disk_space_ok = True
    last_disk_check = 0.0
    queue_cooldown_until: dict[str, float] = {}

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
            previous_total = handled_build_jobs + handled_verify_jobs
            handled_build_jobs += _reap_finished(
                build_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )
            handled_verify_jobs += _reap_finished(
                verify_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )
            if handled_build_jobs + handled_verify_jobs != previous_total:
                _log_handled_job_progress(
                    previous_total=previous_total,
                    build_handled=handled_build_jobs,
                    verify_handled=handled_verify_jobs,
                    build_active=len(build_active),
                    verify_active=len(verify_active),
                    build_queued=build_queue.count,
                    verify_queued=verify_queue.count,
                    interval=progress_log_every_jobs,
                )

            # --- Sweep deferred cgroup removals (non-blocking) ---
            if deferred_cgroup_cleanup:
                _sweep_deferred_cgroups(deferred_cgroup_cleanup)

            # --- Detect build phase completion ---
            # Only relevant when there's a separate verify phase (verify_jobs > 0).
            # Workers pass verify_jobs=0 and the same queue for both, so skip.
            if (
                verify_jobs > 0
                and not build_phase_complete
                and not build_active
                and build_queue.count == 0
            ):
                build_phase_complete = True
                idle_since = time.time()
                logger.info(
                    "=" * 60
                    + "\n  BUILD BACKLOG DRAINED — build queue currently empty"
                    + "\n  Continuing to serve both build and verify queues"
                    + f"\n  Verify queue: {verify_queue.count} pending"
                    + "\n"
                    + "=" * 60
                )

            # --- Idle timeout check (continuous mode only) ---
            if continuous and build_phase_complete and idle_timeout > 0:
                build_idle = not build_active and build_queue.count == 0
                verify_idle = not verify_active and verify_queue.count == 0
                if build_idle and verify_idle:
                    elapsed = time.time() - idle_since
                    if elapsed >= idle_timeout:
                        logger.info(
                            f"Idle timeout reached ({idle_timeout}s) — "
                            "no build or verify activity after backlog drain. Exiting."
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
                    or str(_safe_cwd())
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
                    no_intermediate = (
                        _intermediate_job_count([build_queue, verify_queue]) == 0
                    )
                    if no_queued and no_intermediate:
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
            # Wrap queue-interaction block so corrupted Redis replies
            # (e.g. from a peer evaluator killed on shared Valkey) are
            # retried instead of crashing this supervisor.
            try:
                _refresh_active_claim_leases(redis_conn, build_active)
                _refresh_active_claim_leases(redis_conn, verify_active)
                _recover_stuck_fair_claims([build_queue, verify_queue])

                now = time.time()
                build_eligible = (
                    len(build_active) < build_jobs
                    and queue_cooldown_until.get(build_queue.name, 0.0) <= now
                )
                verify_eligible = (
                    len(verify_active) < verify_jobs
                    and queue_cooldown_until.get(verify_queue.name, 0.0) <= now
                )

                if not build_eligible and not verify_eligible:
                    time.sleep(0.5)
                    consecutive_dequeue_errors = 0
                    continue

                selection_snapshot = clone_fair_scheduler_state(fair_state)
                result = _select_fair_job(
                    redis_conn=redis_conn,
                    build_queues=[build_queue],
                    verify_queues=[verify_queue],
                    build_has_capacity=build_eligible,
                    verify_has_capacity=verify_eligible,
                    state=fair_state,
                    worker_cpu_tag=cpu_tag,
                )

                if not result:
                    if (
                        not continuous
                        and not build_active
                        and not verify_active
                        and _intermediate_job_count([build_queue, verify_queue]) == 0
                        and _queues_blocked_only_by_cpu_tag(
                            [build_queue, verify_queue],
                            worker_cpu_tag=cpu_tag,
                        )
                    ):
                        mismatch_job_ids.clear()
                        mismatch_queue_names.clear()
                        consecutive_cpu_mismatch_requeues += 1
                        if (
                            consecutive_cpu_mismatch_requeues
                            >= NON_CONTINUOUS_CPU_MISMATCH_LIMIT
                        ):
                            logger.warning(
                                "Exiting non-continuous supervisor because only "
                                "cpu_tag-mismatched queued jobs remain for "
                                "compatible workers."
                            )
                            cpu_tag_livelock_detected = True
                            break
                    else:
                        consecutive_cpu_mismatch_requeues = 0
                        mismatch_job_ids.clear()
                        mismatch_queue_names.clear()
                    time.sleep(0.5)
                    consecutive_dequeue_errors = 0
                    continue

                job, queue_obj = result
                consecutive_dequeue_errors = 0
            except (
                OSError,
                TypeError,
                ValueError,
                redis.exceptions.RedisError,
            ) as transient_err:
                consecutive_dequeue_errors += 1
                logger.warning(
                    f"Transient queue error #{consecutive_dequeue_errors}: "
                    f"{transient_err}"
                )
                if consecutive_dequeue_errors >= MAX_CONSECUTIVE_DEQUEUE_ERRORS:
                    raise
                time.sleep(1)
                continue

            # Skip jobs that are already finished or failed (stale queue entries)
            job_status = job.get_status()
            if job_status in ("finished", "failed"):
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                _discard_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                logger.debug(f"Skipping stale job {job.id[:30]} (status={job_status})")
                consecutive_cpu_mismatch_requeues = 0
                mismatch_job_ids.clear()
                mismatch_queue_names.clear()
                continue
            if not _matches_cpu_tag(job, cpu_tag):
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                consecutive_cpu_mismatch_requeues += 1
                mismatch_job_ids.add(job.id)
                mismatch_queue_names.add(queue_obj.name)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                if build_eligible and verify_eligible:
                    queue_cooldown_until[queue_obj.name] = time.time() + 0.1
                logger.debug(
                    f"Re-queued job {job.id[:8]} due to cpu_tag mismatch: "
                    f"job={job.meta.get('cpu_tag')!r}, worker={cpu_tag!r}"
                )
                if (
                    not continuous
                    and not build_active
                    and not verify_active
                    and mismatch_job_ids
                    and consecutive_cpu_mismatch_requeues
                    >= NON_CONTINUOUS_CPU_MISMATCH_LIMIT
                ):
                    non_empty_queue_names = {
                        q.name for q in (build_queue, verify_queue) if q.count > 0
                    }
                    all_pending_unschedulable = bool(
                        non_empty_queue_names
                    ) and non_empty_queue_names.issubset(mismatch_queue_names)
                    if not all_pending_unschedulable:
                        queue_cooldown_until[queue_obj.name] = time.time() + 1.0
                        time.sleep(0.05)
                        continue
                    logger.warning(
                        "Exiting non-continuous supervisor after repeated cpu_tag "
                        "mismatches to avoid requeue livelock; jobs remain queued "
                        "for compatible workers."
                    )
                    cpu_tag_livelock_detected = True
                    break
                time.sleep(0.05)
                continue

            consecutive_cpu_mismatch_requeues = 0
            mismatch_job_ids.clear()
            mismatch_queue_names.clear()
            is_build = queue_obj.name == build_queue_name
            queue_label = "build" if is_build else "verify"
            cpu_count = (
                resolved_build_cores_per_job
                if is_build
                else resolved_verify_cores_per_job
            )

            # Allocate CPUs
            cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None

            if cpu_pool is not None and cpus is None:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
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

                    cgroup_name = _make_unique_cgroup_name(
                        queue_label, worker_num, job.id
                    )
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
                p = _mp_ctx.Process(
                    target=job_runner,
                    args=(redis_host, child_name, job.id),
                    name=f"ci-{queue_label}-{worker_num}",
                )
                p.start()

                if cgroup_path is not None:
                    os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)

                if p.pid is None:
                    raise RuntimeError("Child process started without PID")

                if not p.is_alive():
                    p.join(timeout=0.1)
                    early_status = job.get_status()
                    if early_status in ("finished", "failed"):
                        if early_status == "finished":
                            _enqueue_dependents_for_job(redis_conn, job.id)
                        if cpu_pool and cpus:
                            cpu_pool.release(cpus)
                        if cgroup_path is not None:
                            from crsbench.utils.cgroup import cleanup_cgroup

                            cleanup_cgroup(cgroup_path, force=True)
                        used_worker_nums.discard(worker_num)
                        logger.warning(
                            f"Child for job {job.id[:8]} exited immediately "
                            f"(status={early_status}); skipping requeue."
                        )
                        previous_total = handled_build_jobs + handled_verify_jobs
                        if is_build:
                            handled_build_jobs += 1
                        else:
                            handled_verify_jobs += 1
                        _log_handled_job_progress(
                            previous_total=previous_total,
                            build_handled=handled_build_jobs,
                            verify_handled=handled_verify_jobs,
                            build_active=len(build_active),
                            verify_active=len(verify_active),
                            build_queued=build_queue.count,
                            verify_queued=verify_queue.count,
                            interval=progress_log_every_jobs,
                        )
                        continue
                    raise RuntimeError(
                        f"Child for job {job.id[:8]} exited before registration "
                        f"(status={early_status}); re-enqueuing."
                    )

                entry = WorkerEntry(
                    p,
                    cpus or [],
                    job.id,
                    worker_num,
                    cgroup_path,
                    _claim_lease_key(queue_obj, job.id),
                )
                if is_build:
                    build_active[p.pid] = entry
                else:
                    verify_active[p.pid] = entry

                logger.info(
                    f"Started {queue_label} job {job.id[:8]} (PID: {p.pid})"
                    + (f" with {len(cpus)} CPUs: {cpuset_str}" if cpus else "")
                )
            except OSError as spawn_err:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
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
                if cgroup_path is not None:
                    try:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path, force=True)
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to cleanup cgroup after spawn failure: {cleanup_err}"
                        )
                if cpu_pool and cpus:
                    cpu_pool.release(cpus)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                time.sleep(0.5)
                continue
            except Exception as spawn_err:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                logger.warning(
                    f"Child startup failed for worker {worker_num}: {spawn_err}. "
                    "Re-enqueuing job."
                )
                os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)
                if cgroup_path is not None:
                    try:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path, force=True)
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to cleanup cgroup after startup failure: "
                            f"{cleanup_err}"
                        )
                if cpu_pool and cpus:
                    cpu_pool.release(cpus)
                used_worker_nums.discard(worker_num)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                time.sleep(0.5)
                continue

    except KeyboardInterrupt:
        logger.info("\nCI supervisor interrupted, terminating workers...")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
        _cleanup_stale_rq_workers(redis_host, build_queue_name, verify_queue_name)
        return 0
    except Exception:
        logger.exception("CI supervisor error")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
        _cleanup_stale_rq_workers(redis_host, build_queue_name, verify_queue_name)
        return 3

    _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
    _cleanup_stale_rq_workers(redis_host, build_queue_name, verify_queue_name)
    if cpu_tag_livelock_detected:
        return CPU_TAG_MISMATCH_EXIT_CODE
    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def run_multi_queue_supervisor(
    redis_host: str,
    build_queue_names: list[str],
    verify_queue_names: list[str],
    worker_name: str,
    build_jobs: int,
    build_cores_per_job: Optional[int],
    verify_jobs: int,
    job_runner: JobRunner,
    *,
    verify_cores_per_job: Optional[int] = None,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    minimum_disk_size: str = "10GB",
    disk_check_interval: int = 60,
    continuous: bool = True,
    idle_timeout: int = 0,
    queue_refresher: Optional[QueueRefresher] = None,
    queue_refresh_interval: int = 5,
    cpu_tag: Optional[str] = None,
    progress_log_every_jobs: int = 0,
) -> int:
    """Multi-queue supervisor for configless workers and evaluators.

    Similar to ``run_ci_supervisor`` but accepts *lists* of queue names,
    enabling a single supervisor to serve multiple experiments concurrently.

    Runnable work is selected fairly across build and verify queue classes.
    Build phase is considered complete when ALL build queues are empty.

    Args:
        redis_host: Redis server hostname.
        build_queue_names: List of Redis build queue names.
        verify_queue_names: List of Redis verify queue names.
        worker_name: Base worker name for child processes.
        build_jobs: Max concurrent build jobs.
        build_cores_per_job: CPUs per build job.
        verify_jobs: Max concurrent verify jobs.
        job_runner: Callable to execute a single job in a child process.
        verify_cores_per_job: CPUs per verify job.
        use_cpuset: Enable CPU affinity.
        use_cgroups: Create per-job cgroups.
        cores: CPU cores for pool.
        skip_cpus: CPUs to exclude.
        minimum_disk_size: Minimum free disk space before pausing.
        disk_check_interval: Seconds between disk space checks.
        continuous: Keep running when queues are empty.
        idle_timeout: Exit after N seconds idle post-build-phase.
        queue_refresher: Optional callback to refresh queue names at runtime.
        queue_refresh_interval: Seconds between refresh attempts.
        cpu_tag: Optional worker capability tag. Jobs with mismatched
            ``job.meta['cpu_tag']`` are re-queued.
        progress_log_every_jobs: Emit a handled-job progress log whenever
            this many completed jobs have been reached. ``0`` disables the
            progress log.

    Returns:
        Exit code (0 for success).
    """
    from crsbench.utils.cpu_pool import CPUPool, format_cpuset
    from crsbench.utils.size_parser import parse_size_to_bytes

    os.environ["CRSBENCH_SUPERVISOR"] = "1"
    resolved_build_cores_per_job = (
        build_cores_per_job
        if build_cores_per_job is not None
        else (
            auto_cores_per_job(build_jobs, cores=cores, skip_cpus=skip_cpus)
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )
    resolved_verify_cores_per_job = (
        verify_cores_per_job
        if verify_cores_per_job is not None
        else (
            auto_cores_per_job(max(verify_jobs, 1), cores=cores, skip_cpus=skip_cpus)
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )

    logger.info("Starting multi-queue supervisor...")
    logger.info(f"Build queues: {build_queue_names}")
    logger.info(f"Verify queues: {verify_queue_names}")
    logger.info(
        f"Build concurrency: {build_jobs} jobs x {resolved_build_cores_per_job} CPUs each"
    )
    logger.info(
        f"Verify concurrency: {verify_jobs} jobs x {resolved_verify_cores_per_job} CPUs each"
    )
    if cpu_tag:
        logger.info(f"CPU tag filter enabled: {cpu_tag}")
    if queue_refresher is not None:
        logger.info(
            f"Runtime queue refresh enabled (interval={queue_refresh_interval}s)"
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

    # Build set for fast membership checks
    build_queue_name_set = set(build_queue_names)

    # Tracking state
    build_active: dict[int, WorkerEntry] = {}
    verify_active: dict[int, WorkerEntry] = {}
    deferred_cgroup_cleanup: list[Path] = []

    used_worker_nums: set[int] = set()
    max_total = build_jobs + verify_jobs
    build_phase_complete = False
    idle_since: float = 0.0
    consecutive_cpu_mismatch_requeues = 0
    consecutive_dequeue_errors = 0
    mismatch_job_ids: set[str] = set()
    mismatch_queue_names: set[str] = set()
    cpu_tag_livelock_detected = False
    handled_build_jobs = 0
    handled_verify_jobs = 0
    fair_state = FairSchedulerState()

    # Disk space state
    minimum_disk_bytes = parse_size_to_bytes(minimum_disk_size)
    disk_space_ok = True
    last_disk_check = 0.0
    last_queue_refresh = 0.0
    queue_cooldown_until: dict[str, float] = {}

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        # Create queue objects
        build_queues = [
            rq.Queue(name, connection=redis_conn) for name in build_queue_names
        ]
        verify_queues = [
            rq.Queue(name, connection=redis_conn) for name in verify_queue_names
        ]

        logger.info(
            f"Multi-queue supervisor connected: "
            f"{len(build_queues)} build + {len(verify_queues)} verify queues"
        )
        if cpu_pool:
            logger.info(f"CPU pool: {cpu_pool.total_cpus} CPUs")

        while True:
            # --- Cleanup finished workers ---
            previous_total = handled_build_jobs + handled_verify_jobs
            handled_build_jobs += _reap_finished(
                build_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )
            handled_verify_jobs += _reap_finished(
                verify_active,
                cpu_pool,
                used_worker_nums,
                deferred_cgroup_cleanup,
                redis_conn=redis_conn,
            )
            if handled_build_jobs + handled_verify_jobs != previous_total:
                _log_handled_job_progress(
                    previous_total=previous_total,
                    build_handled=handled_build_jobs,
                    verify_handled=handled_verify_jobs,
                    build_active=len(build_active),
                    verify_active=len(verify_active),
                    build_queued=sum(bq.count for bq in build_queues),
                    verify_queued=sum(vq.count for vq in verify_queues),
                    interval=progress_log_every_jobs,
                )

            # --- Sweep deferred cgroup removals ---
            if deferred_cgroup_cleanup:
                _sweep_deferred_cgroups(deferred_cgroup_cleanup)

            # --- Refresh queue set from registry (configless mode) ---
            if (
                queue_refresher
                and time.time() - last_queue_refresh >= queue_refresh_interval
            ):
                last_queue_refresh = time.time()
                try:
                    refreshed_build, refreshed_verify = queue_refresher(redis_conn)
                    refreshed_build = sorted(set(refreshed_build))
                    refreshed_verify = sorted(set(refreshed_verify))
                    if (
                        refreshed_build != build_queue_names
                        or refreshed_verify != verify_queue_names
                    ):
                        old_build = build_queue_names
                        old_verify = verify_queue_names
                        build_queue_names = refreshed_build
                        verify_queue_names = refreshed_verify
                        build_queue_name_set = set(build_queue_names)
                        build_queues = [
                            rq.Queue(name, connection=redis_conn)
                            for name in build_queue_names
                        ]
                        verify_queues = [
                            rq.Queue(name, connection=redis_conn)
                            for name in verify_queue_names
                        ]
                        fair_state.next_queue_index_by_class["build"] = 0
                        fair_state.next_queue_index_by_class["verify"] = 0
                        # New build queues may appear after backlog was drained.
                        build_phase_complete = False
                        idle_since = time.time()
                        logger.info(
                            "Queue set updated from registry: "
                            f"build {old_build} -> {build_queue_names}, "
                            f"verify {old_verify} -> {verify_queue_names}"
                        )
                except Exception as exc:
                    logger.warning(f"Queue refresh failed: {exc}")

            # --- Detect build phase completion ---
            if (
                verify_jobs > 0
                and not build_phase_complete
                and not build_active
                and all(bq.count == 0 for bq in build_queues)
            ):
                build_phase_complete = True
                idle_since = time.time()
                total_verify = sum(vq.count for vq in verify_queues)
                logger.info(
                    "=" * 60
                    + "\n  BUILD BACKLOG DRAINED — build queues currently empty"
                    + "\n  Continuing to serve both build and verify queues"
                    + f"\n  Verify queues: {total_verify} pending"
                    + "\n"
                    + "=" * 60
                )

            # --- Idle timeout check ---
            if continuous and build_phase_complete and idle_timeout > 0:
                all_build_empty = all(bq.count == 0 for bq in build_queues)
                all_verify_empty = all(vq.count == 0 for vq in verify_queues)
                if (
                    not build_active
                    and all_build_empty
                    and not verify_active
                    and all_verify_empty
                ):
                    elapsed = time.time() - idle_since
                    if elapsed >= idle_timeout:
                        logger.info(
                            f"Idle timeout reached ({idle_timeout}s) — "
                            "no build or verify activity after backlog drain. Exiting."
                        )
                        break
                else:
                    idle_since = time.time()

            # --- Disk space check ---
            current_time = time.time()
            if current_time - last_disk_check >= disk_check_interval:
                filestore_path = Path(
                    os.environ.get("CRSBENCH_WORKER_EXPERIMENT_FILESTORE")
                    or str(_safe_cwd())
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
            if not continuous:
                no_active_workers = not build_active and not verify_active
                if no_active_workers:
                    all_queued_empty = all(
                        bq.count == 0 for bq in build_queues
                    ) and all(vq.count == 0 for vq in verify_queues)
                    no_intermediate = (
                        _intermediate_job_count([*build_queues, *verify_queues]) == 0
                    )
                    if all_queued_empty and no_intermediate:
                        all_deferred_empty = all(
                            bq.deferred_job_registry.count == 0 for bq in build_queues
                        ) and all(
                            vq.deferred_job_registry.count == 0 for vq in verify_queues
                        )
                        if all_deferred_empty:
                            logger.info(
                                "All queues empty and no active workers — exiting"
                            )
                            break

            # --- Determine which queues have capacity ---
            try:
                _refresh_active_claim_leases(redis_conn, build_active)
                _refresh_active_claim_leases(redis_conn, verify_active)
                _recover_stuck_fair_claims([*build_queues, *verify_queues])

                now = time.time()
                eligible_build_queues = [
                    bq
                    for bq in build_queues
                    if len(build_active) < build_jobs
                    and queue_cooldown_until.get(bq.name, 0.0) <= now
                ]
                eligible_verify_queues = [
                    vq
                    for vq in verify_queues
                    if len(verify_active) < verify_jobs
                    and queue_cooldown_until.get(vq.name, 0.0) <= now
                ]

                if not eligible_build_queues and not eligible_verify_queues:
                    time.sleep(0.5)
                    consecutive_dequeue_errors = 0
                    continue

                selection_snapshot = clone_fair_scheduler_state(fair_state)
                result = _select_fair_job(
                    redis_conn=redis_conn,
                    build_queues=eligible_build_queues,
                    verify_queues=eligible_verify_queues,
                    build_has_capacity=bool(eligible_build_queues),
                    verify_has_capacity=bool(eligible_verify_queues),
                    state=fair_state,
                    worker_cpu_tag=cpu_tag,
                )

                if not result:
                    if (
                        not continuous
                        and not build_active
                        and not verify_active
                        and _intermediate_job_count([*build_queues, *verify_queues])
                        == 0
                        and _queues_blocked_only_by_cpu_tag(
                            [*build_queues, *verify_queues],
                            worker_cpu_tag=cpu_tag,
                        )
                    ):
                        mismatch_job_ids.clear()
                        mismatch_queue_names.clear()
                        consecutive_cpu_mismatch_requeues += 1
                        if (
                            consecutive_cpu_mismatch_requeues
                            >= NON_CONTINUOUS_CPU_MISMATCH_LIMIT
                        ):
                            logger.warning(
                                "Exiting non-continuous multi-queue supervisor "
                                "because only cpu_tag-mismatched queued jobs "
                                "remain for compatible workers."
                            )
                            cpu_tag_livelock_detected = True
                            break
                    else:
                        consecutive_cpu_mismatch_requeues = 0
                        mismatch_job_ids.clear()
                        mismatch_queue_names.clear()
                    time.sleep(0.5)
                    consecutive_dequeue_errors = 0
                    continue

                job, queue_obj = result
                consecutive_dequeue_errors = 0
            except (
                OSError,
                TypeError,
                ValueError,
                redis.exceptions.RedisError,
            ) as transient_err:
                consecutive_dequeue_errors += 1
                logger.warning(
                    f"Transient queue error #{consecutive_dequeue_errors}: "
                    f"{transient_err}"
                )
                if consecutive_dequeue_errors >= MAX_CONSECUTIVE_DEQUEUE_ERRORS:
                    raise
                time.sleep(1)
                continue

            # Skip stale jobs
            job_status = job.get_status()
            if job_status in ("finished", "failed"):
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                _discard_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                logger.debug(f"Skipping stale job {job.id[:30]} (status={job_status})")
                consecutive_cpu_mismatch_requeues = 0
                mismatch_job_ids.clear()
                mismatch_queue_names.clear()
                continue
            if not _matches_cpu_tag(job, cpu_tag):
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                consecutive_cpu_mismatch_requeues += 1
                mismatch_job_ids.add(job.id)
                mismatch_queue_names.add(queue_obj.name)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                if eligible_build_queues and eligible_verify_queues:
                    queue_cooldown_until[queue_obj.name] = time.time() + 0.1
                logger.debug(
                    f"Re-queued job {job.id[:8]} due to cpu_tag mismatch: "
                    f"job={job.meta.get('cpu_tag')!r}, worker={cpu_tag!r}"
                )
                if (
                    not continuous
                    and not build_active
                    and not verify_active
                    and mismatch_job_ids
                    and consecutive_cpu_mismatch_requeues
                    >= NON_CONTINUOUS_CPU_MISMATCH_LIMIT
                ):
                    non_empty_queue_names = {
                        q.name for q in (*build_queues, *verify_queues) if q.count > 0
                    }
                    all_pending_unschedulable = bool(
                        non_empty_queue_names
                    ) and non_empty_queue_names.issubset(mismatch_queue_names)
                    if not all_pending_unschedulable:
                        queue_cooldown_until[queue_obj.name] = time.time() + 1.0
                        time.sleep(0.05)
                        continue
                    logger.warning(
                        "Exiting non-continuous multi-queue supervisor after "
                        "repeated cpu_tag mismatches to avoid requeue livelock; "
                        "jobs remain queued for compatible workers."
                    )
                    cpu_tag_livelock_detected = True
                    break
                time.sleep(0.05)
                continue

            consecutive_cpu_mismatch_requeues = 0
            mismatch_job_ids.clear()
            mismatch_queue_names.clear()
            is_build = queue_obj.name in build_queue_name_set
            queue_label = "build" if is_build else "verify"
            cpu_count = (
                resolved_build_cores_per_job
                if is_build
                else resolved_verify_cores_per_job
            )

            # Allocate CPUs
            cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None

            if cpu_pool is not None and cpus is None:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
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

            try:
                cgroup_path: Optional[Path] = None
                if cgroup_base is not None and cpuset_str:
                    from crsbench.utils.cgroup import (
                        cgroup_path_for_docker,
                        create_cgroup,
                    )

                    cgroup_name = _make_unique_cgroup_name(
                        queue_label, worker_num, job.id
                    )
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
                p = _mp_ctx.Process(
                    target=job_runner,
                    args=(redis_host, child_name, job.id),
                    name=f"mq-{queue_label}-{worker_num}",
                )
                p.start()

                if cgroup_path is not None:
                    os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)

                if p.pid is None:
                    raise RuntimeError("Child process started without PID")

                if not p.is_alive():
                    p.join(timeout=0.1)
                    early_status = job.get_status()
                    if early_status in ("finished", "failed"):
                        if early_status == "finished":
                            _enqueue_dependents_for_job(redis_conn, job.id)
                        if cpu_pool and cpus:
                            cpu_pool.release(cpus)
                        if cgroup_path is not None:
                            from crsbench.utils.cgroup import cleanup_cgroup

                            cleanup_cgroup(cgroup_path, force=True)
                        used_worker_nums.discard(worker_num)
                        logger.warning(
                            f"Child for job {job.id[:8]} exited immediately "
                            f"(status={early_status}); skipping requeue."
                        )
                        previous_total = handled_build_jobs + handled_verify_jobs
                        if is_build:
                            handled_build_jobs += 1
                        else:
                            handled_verify_jobs += 1
                        _log_handled_job_progress(
                            previous_total=previous_total,
                            build_handled=handled_build_jobs,
                            verify_handled=handled_verify_jobs,
                            build_active=len(build_active),
                            verify_active=len(verify_active),
                            build_queued=sum(bq.count for bq in build_queues),
                            verify_queued=sum(vq.count for vq in verify_queues),
                            interval=progress_log_every_jobs,
                        )
                        continue
                    raise RuntimeError(
                        f"Child for job {job.id[:8]} exited before registration "
                        f"(status={early_status}); re-enqueuing."
                    )

                entry = WorkerEntry(
                    p,
                    cpus or [],
                    job.id,
                    worker_num,
                    cgroup_path,
                    _claim_lease_key(queue_obj, job.id),
                )
                if is_build:
                    build_active[p.pid] = entry
                else:
                    verify_active[p.pid] = entry

                logger.info(
                    f"Started {queue_label} job {job.id[:8]} (PID: {p.pid})"
                    + (f" with {len(cpus)} CPUs: {cpuset_str}" if cpus else "")
                )
            except OSError as spawn_err:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                logger.warning(
                    f"Failed to spawn worker {worker_num}: {spawn_err}. "
                    "Re-enqueuing job."
                )
                os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)
                if cgroup_path is not None:
                    try:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path, force=True)
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to cleanup cgroup after spawn failure: {cleanup_err}"
                        )
                if cpu_pool and cpus:
                    cpu_pool.release(cpus)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                time.sleep(0.5)
                continue
            except Exception as spawn_err:
                restore_fair_scheduler_state(fair_state, selection_snapshot)
                logger.warning(
                    f"Child startup failed for worker {worker_num}: {spawn_err}. "
                    "Re-enqueuing job."
                )
                os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
                os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)
                if cgroup_path is not None:
                    try:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path, force=True)
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to cleanup cgroup after startup failure: "
                            f"{cleanup_err}"
                        )
                if cpu_pool and cpus:
                    cpu_pool.release(cpus)
                used_worker_nums.discard(worker_num)
                _restore_claimed_job(redis_conn, queue=queue_obj, job_id=job.id)
                time.sleep(0.5)
                continue

    except KeyboardInterrupt:
        logger.info("\nMulti-queue supervisor interrupted, terminating workers...")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
        _cleanup_stale_rq_workers(redis_host, build_queue_names, verify_queue_names)
        return 0
    except Exception:
        logger.exception("Multi-queue supervisor error")
        _terminate_all(build_active, cpu_pool)
        _terminate_all(verify_active, cpu_pool)
        _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
        _cleanup_stale_rq_workers(redis_host, build_queue_names, verify_queue_names)
        return 3

    _force_cleanup_deferred_cgroups(deferred_cgroup_cleanup)
    _cleanup_stale_rq_workers(redis_host, build_queue_names, verify_queue_names)
    if cpu_tag_livelock_detected:
        return CPU_TAG_MISMATCH_EXIT_CODE
    return 0


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
        logger.opt(exception=True).debug("Failed to enqueue dependents for {}", job_id)


def _matches_cpu_tag(job: "rq.job.Job", worker_cpu_tag: Optional[str]) -> bool:
    """Return whether a job is runnable on this worker by CPU tag."""
    normalized_worker_tag = normalize_cpu_tag(worker_cpu_tag)
    raw_job_tag = job.meta.get("cpu_tag")
    normalized_job_tag = normalize_cpu_tag(str(raw_job_tag) if raw_job_tag else None)

    # Untagged workers only run untagged jobs.
    if not normalized_worker_tag:
        return normalized_job_tag is None

    # Tagged workers can run untagged jobs plus matching tagged jobs.
    if normalized_job_tag is None:
        return True
    return normalized_job_tag == normalized_worker_tag


def _intermediate_job_count(queues: list["rq.Queue"]) -> int:
    return sum(len(list(queue.intermediate_queue.get_job_ids())) for queue in queues)


def _queues_blocked_only_by_cpu_tag(
    queues: list["rq.Queue"],
    *,
    worker_cpu_tag: Optional[str],
) -> bool:
    """Return whether queued jobs exist but none are locally runnable by cpu_tag."""
    if not queues:
        return False

    queue_job_ids = [list(queue.get_job_ids()) for queue in queues]
    all_job_ids = [job_id for job_ids in queue_job_ids for job_id in job_ids]
    if not all_job_ids:
        return False

    jobs = rq.job.Job.fetch_many(all_job_ids, connection=queues[0].connection)
    jobs_by_id = {job.id: job for job in jobs if job is not None}

    saw_mismatch = False
    saw_runnable = False
    for queue, job_ids in zip(queues, queue_job_ids, strict=False):
        for job_id in job_ids:
            job = jobs_by_id.get(job_id)
            if job is None:
                _remove_stale_queue_job_id(queue, job_id)
                continue
            if _matches_cpu_tag(job, worker_cpu_tag):
                saw_runnable = True
            else:
                saw_mismatch = True

    return saw_mismatch and not saw_runnable


def _refresh_active_claim_leases(
    redis_conn: "Redis",
    active: dict[int, WorkerEntry],
) -> None:
    for entry in active.values():
        if entry.claim_lease_key:
            redis_conn.expire(entry.claim_lease_key, FAIR_CLAIM_LEASE_TTL_SECONDS)


def _recover_stuck_fair_claims(queues: list["rq.Queue"]) -> None:
    """Requeue abandoned fair-claimed jobs that never reached execution start."""
    for queue in queues:
        intermediate = queue.intermediate_queue
        for job_id in list(intermediate.get_job_ids()):
            if job_id in queue.started_job_registry:
                continue
            if queue.connection.exists(_claim_lease_key(queue, job_id)):
                continue

            job = queue.fetch_job(job_id)
            if job is None:
                _discard_claimed_job(queue.connection, queue=queue, job_id=job_id)
                continue

            job_status = job.get_status()
            if job_status in ("finished", "failed", "stopped", "canceled"):
                _discard_claimed_job(queue.connection, queue=queue, job_id=job_id)
                continue

            restored = _restore_claimed_job(
                queue.connection, queue=queue, job_id=job_id
            )
            if not restored:
                continue

            if job_status != "queued":
                job.set_status(JobStatus.QUEUED)
            logger.warning(
                f"Recovered abandoned fair-claimed job {job_id} from "
                f"{queue.name} intermediate queue."
            )


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


def _progress_milestones_crossed(
    previous_total: int,
    current_total: int,
    *,
    interval: int,
) -> list[int]:
    """Return handled-job milestones crossed between two totals."""
    if interval <= 0 or current_total <= previous_total:
        return []

    previous_bucket = previous_total // interval
    current_bucket = current_total // interval
    return [
        bucket * interval for bucket in range(previous_bucket + 1, current_bucket + 1)
    ]


def _log_handled_job_progress(
    *,
    previous_total: int,
    build_handled: int,
    verify_handled: int,
    build_active: int,
    verify_active: int,
    build_queued: int,
    verify_queued: int,
    interval: int,
) -> None:
    """Emit handled-job milestone logs when a new progress bucket is crossed."""
    total_handled = build_handled + verify_handled
    for milestone in _progress_milestones_crossed(
        previous_total, total_handled, interval=interval
    ):
        logger.info(
            f"Handled {milestone} jobs so far "
            f"(build={build_handled}, verify={verify_handled}, "
            f"active: build={build_active}, verify={verify_active}, "
            f"queued: build={build_queued}, verify={verify_queued})"
        )


def _reap_finished(
    active: dict[int, WorkerEntry],
    cpu_pool: Optional[object],
    used_worker_nums: set[int],
    deferred_cgroup_cleanup: list[Path],
    redis_conn: Optional[Redis] = None,
) -> int:
    """Clean up finished child processes and release their resources.

    After a child completes, enqueues any dependent jobs via RQ's
    ``Queue.enqueue_dependents()`` so cross-queue dependencies resolve
    immediately rather than waiting for periodic recovery sweeps.

    Cgroup removal is attempted once without retries to avoid blocking
    the supervisor loop.  If the cgroup is still busy (Docker shim
    processes haven't exited yet), the path is deferred for later sweep.
    """
    handled = 0
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
            handled += 1
    return handled


def _sweep_deferred_cgroups(deferred: list[Path]) -> None:
    """Try to remove previously deferred cgroup paths (single silent attempt)."""
    remaining: list[Path] = []
    for cg_path in deferred:
        if not _try_remove_cgroup(cg_path):
            remaining.append(cg_path)
    deferred.clear()
    deferred.extend(remaining)


def _force_cleanup_deferred_cgroups(deferred: list[Path]) -> None:
    """Force-remove any deferred cgroups during supervisor shutdown."""
    if not deferred:
        return
    from crsbench.utils.cgroup import cleanup_cgroup

    remaining: list[Path] = []
    for cg_path in deferred:
        try:
            cleaned = cleanup_cgroup(cg_path, force=True)
            if not cleaned:
                logger.warning(
                    f"Deferred cgroup cleanup reported failure for {cg_path}"
                )
                remaining.append(cg_path)
        except Exception as e:
            logger.warning(f"Deferred cgroup cleanup failed for {cg_path}: {e}")
            remaining.append(cg_path)
    deferred.clear()
    deferred.extend(remaining)


def _cleanup_stale_rq_workers(
    redis_host: str,
    *queue_names: Union[str, list[str]],
) -> None:
    """Remove dead RQ workers from the Redis worker registry.

    Accepts individual queue name strings and/or lists of queue names.
    After worker processes are terminated, their RQ entries may linger in
    Redis until the heartbeat TTL expires.  This helper deregisters any
    worker whose OS process no longer exists.
    """
    if not REDIS_AVAILABLE:
        return

    from crsbench.distributed.queue import create_redis_connection

    # Flatten mixed str / list[str] arguments.
    flat_names: list[str] = []
    for name in queue_names:
        if isinstance(name, list):
            flat_names.extend(name)
        else:
            flat_names.append(name)

    try:
        conn = create_redis_connection(redis_host)
    except Exception as exc:
        logger.warning(f"Failed to connect to Redis for worker cleanup: {exc}")
        return

    seen_worker_keys: set[str] = set()
    for qname in flat_names:
        try:
            queue = rq.Queue(qname, connection=conn)
            workers = rq.Worker.all(queue=queue)
        except Exception as exc:
            logger.warning(f"Failed to list RQ workers for queue {qname}: {exc}")
            continue

        for worker in workers:
            if worker.key in seen_worker_keys:
                continue
            seen_worker_keys.add(worker.key)
            try:
                worker.refresh()
                pid = worker.pid
                if pid is None:
                    worker.register_death()
                    logger.info(f"Deregistered RQ worker with no PID: {worker.name}")
                    continue
                # Check whether the process is still alive.
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    worker.register_death()
                    logger.info(
                        f"Deregistered stale RQ worker: {worker.name} (PID {pid})"
                    )
                except Exception as inner:
                    logger.warning(
                        f"Failed to deregister stale worker {worker.name}: {inner}"
                    )
            except PermissionError:
                pass
            except Exception as exc:
                logger.debug(f"Skipping worker {worker.name} during cleanup: {exc}")


def _terminate_all(
    active: dict[int, WorkerEntry],
    cpu_pool: Optional[object],
) -> None:
    """Terminate and clean up all active workers and their process trees."""
    from crsbench.distributed.worker import _terminate_process_tree

    for entry in active.values():
        try:
            if entry.process.is_alive() and entry.process.pid is not None:
                _terminate_process_tree(entry.process.pid)
        except Exception as e:
            logger.warning(f"Failed to terminate worker process tree: {e}")
    for pid, entry in active.items():
        try:
            entry.process.join(timeout=3)
        except Exception as e:
            logger.warning(f"Failed joining worker (PID: {pid}): {e}")
        if cpu_pool and entry.cpus:
            try:
                cpu_pool.release(entry.cpus)  # type: ignore[union-attr]
            except Exception as e:
                logger.warning(f"Failed to release CPUs {entry.cpus}: {e}")
        if entry.cgroup_path:
            from crsbench.utils.cgroup import cleanup_cgroup

            try:
                cleaned = cleanup_cgroup(entry.cgroup_path, force=True)
                if not cleaned:
                    logger.warning(
                        "Failed to cleanup worker cgroup during shutdown: "
                        f"{entry.cgroup_path}"
                    )
            except Exception as e:
                logger.warning(
                    "Exception during worker cgroup cleanup for "
                    f"{entry.cgroup_path}: {e}"
                )
