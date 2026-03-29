"""Shared live queue monitoring for distributed orchestration commands."""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from crsbench.distributed.queue import (
    get_existing_trial_jobs,
    get_queue_stats,
    get_trial_key,
)
from crsbench.utils import log_progress, log_section, log_summary
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.distributed.registry import RegistryClient

logger = get_logger(__name__)


def _rich_console_available() -> bool:
    """Return True when Rich is installed and stdout is interactive."""
    return (
        importlib.util.find_spec("rich") is not None
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )


@dataclass(frozen=True)
class RunningJobInfo:
    """Human-readable running-job row for the operator monitor."""

    worker_name: str
    crs: str
    benchmark: str
    harness: str
    target_cpv_id: str
    mode: str
    trial_num: str
    phase: str
    elapsed: str


@dataclass(frozen=True)
class QueueJobEntry:
    """Experiment-scoped job row derived directly from RQ queue registries."""

    job_id: str
    trial_key: str
    state: str
    claimed_by: str | None
    retry_count: int


@dataclass(frozen=True)
class QueueMonitorSnapshot:
    """Experiment-scoped queue snapshot used by both monitor renderers."""

    stats: dict[str, int]
    running_jobs: list[RunningJobInfo]


@dataclass
class QueueMonitorCallbacks:
    """Optional side-effect hooks for tracked jobs in local-run mode."""

    on_job_finished: Callable[[object], bool | None] | None = None
    on_job_failed: Callable[[object], bool | None] | None = None


def get_experiment_queue_stats(queue, experiment_name: str) -> dict[str, int]:
    """Return experiment-scoped queue counts plus global worker visibility."""
    global_stats = get_queue_stats(queue)
    existing = get_existing_trial_jobs(queue, experiment_name=experiment_name)
    return {
        "queued": len(existing["queued"]),
        "started": len(existing["started"]),
        "finished": len(existing["finished"]),
        "failed": len(existing["failed"]),
        "workers": int(global_stats.get("workers", 0)),
    }


def build_monitor_snapshot(queue, experiment_name: str) -> QueueMonitorSnapshot:
    """Build one experiment-scoped operator snapshot from Redis/RQ state."""
    existing = get_existing_trial_jobs(queue, experiment_name=experiment_name)
    stats = get_experiment_queue_stats(queue, experiment_name)
    running_jobs: list[RunningJobInfo] = []

    for job in existing["started"]:
        refresh = getattr(job, "refresh", None)
        if callable(refresh):
            refresh()
        meta = getattr(job, "meta", {}) or {}
        phase_started_at = meta.get("phase_started_at")
        elapsed = ""
        if isinstance(phase_started_at, int | float):
            elapsed_sec = max(0, int(time.time() - phase_started_at))
            mins, secs = divmod(elapsed_sec, 60)
            elapsed = f"{mins}m{secs}s"
        running_jobs.append(
            RunningJobInfo(
                worker_name=str(meta.get("worker_name", "?")),
                crs=str(meta.get("crs", "?")),
                benchmark=str(meta.get("benchmark", "?")),
                harness=str(meta.get("harness", "?")),
                target_cpv_id=str(meta.get("target_cpv_id", "-")),
                mode=str(meta.get("mode", "?")),
                trial_num=str(meta.get("trial_num", "?")),
                phase=str(meta.get("phase", "queued")),
                elapsed=elapsed,
            )
        )

    return QueueMonitorSnapshot(stats=stats, running_jobs=running_jobs)


def list_queue_job_entries(queue, experiment_name: str) -> list[QueueJobEntry]:
    """Return queue-derived per-job status rows for one experiment."""
    existing = get_existing_trial_jobs(queue, experiment_name=experiment_name)
    entries: list[QueueJobEntry] = []
    state_map = (
        ("queued", "queued"),
        ("deferred", "deferred"),
        ("scheduled", "scheduled"),
        ("started", "running"),
        ("finished", "completed"),
        ("failed", "failed"),
    )

    for bucket_name, state in state_map:
        for job in existing[bucket_name]:
            refresh = getattr(job, "refresh", None)
            if callable(refresh):
                refresh()
            meta = getattr(job, "meta", {}) or {}
            retry_count = meta.get("retry_count", 0)
            if not isinstance(retry_count, int):
                retry_count = 0
            claimed_by = meta.get("worker_name") if state == "running" else None
            if not isinstance(claimed_by, str) or not claimed_by.strip():
                claimed_by = None
            entries.append(
                QueueJobEntry(
                    job_id=str(getattr(job, "id", "") or ""),
                    trial_key=get_trial_key(job),
                    state=state,
                    claimed_by=claimed_by,
                    retry_count=retry_count,
                )
            )

    entries.sort(key=lambda entry: entry.job_id)
    return entries


def monitor_queue(
    queue,
    experiment_name: str,
    *,
    tracked_job_ids: list[str] | None = None,
    tracked_jobs: list[object] | None = None,
    total_jobs: int | None = None,
    disk_skipped: int = 0,
    registry: "RegistryClient | None" = None,
    callbacks: QueueMonitorCallbacks | None = None,
    use_rich: bool | None = None,
    poll_interval: float = 3.0,
    exit_when_idle: bool = True,
) -> None:
    """Run the shared operator queue monitor until completion."""
    del tracked_job_ids  # Reserved for future queue-only tracked attach mode.

    if use_rich is None:
        use_rich = _rich_console_available()

    callbacks = callbacks or QueueMonitorCallbacks()

    if use_rich:
        _monitor_queue_rich(
            queue,
            experiment_name,
            tracked_jobs=tracked_jobs,
            total_jobs=total_jobs,
            disk_skipped=disk_skipped,
            registry=registry,
            callbacks=callbacks,
            poll_interval=poll_interval,
            exit_when_idle=exit_when_idle,
        )
        return

    _monitor_queue_basic(
        queue,
        experiment_name,
        tracked_jobs=tracked_jobs,
        total_jobs=total_jobs,
        disk_skipped=disk_skipped,
        registry=registry,
        callbacks=callbacks,
        poll_interval=poll_interval,
        exit_when_idle=exit_when_idle,
    )


def _default_total(snapshot: QueueMonitorSnapshot) -> int:
    stats = snapshot.stats
    return stats["queued"] + stats["started"] + stats["finished"] + stats["failed"]


def _display_total(snapshot: QueueMonitorSnapshot, total_jobs: int | None) -> int:
    return total_jobs if total_jobs is not None else _default_total(snapshot)


def _process_tracked_jobs(
    tracked_jobs: list[object] | None,
    *,
    callbacks: QueueMonitorCallbacks,
    seen_finished: set[str],
    seen_failed: set[str],
) -> tuple[int, int]:
    if not tracked_jobs:
        return 0, 0

    for job in tracked_jobs:
        refresh = getattr(job, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:
                job_id = getattr(job, "id", "<unknown>")
                logger.warning(
                    f"Failed to refresh tracked job {job_id}; will retry on next scan: {exc}"
                )
                continue
        job_id = getattr(job, "id", None)
        if not isinstance(job_id, str):
            continue
        is_finished = bool(getattr(job, "is_finished", False))
        is_failed = bool(getattr(job, "is_failed", False))
        if not is_finished and not is_failed:
            seen_finished.discard(job_id)
            seen_failed.discard(job_id)
            continue
        if is_finished:
            seen_failed.discard(job_id)
        elif is_failed:
            seen_finished.discard(job_id)
        if is_finished and job_id not in seen_finished:
            processed = True
            if callbacks.on_job_finished is not None:
                processed = callbacks.on_job_finished(job) is not False
            if processed:
                seen_finished.add(job_id)
        elif is_failed and job_id not in seen_failed:
            processed = True
            if callbacks.on_job_failed is not None:
                processed = callbacks.on_job_failed(job) is not False
            if processed:
                seen_failed.add(job_id)

    return len(seen_finished), len(seen_failed)


def _renew_registry(
    registry: "RegistryClient | None",
    *,
    experiment_name: str,
    last_renew: float,
) -> float:
    if registry is None:
        return last_renew
    if time.monotonic() - last_renew < 60:
        return last_renew
    if not registry.renew(experiment_name):
        logger.warning("Experiment lock lost — another run may have taken over")
    return time.monotonic()


def _log_snapshot_basic(
    snapshot: QueueMonitorSnapshot,
    *,
    experiment_name: str,
    total_jobs: int,
    disk_skipped: int,
) -> None:
    log_section(f"Experiment: {experiment_name}", width=60)
    logger.info(f"Workers connected: {snapshot.stats.get('workers', 0)}")
    status_dict: dict[str, int] = {
        "queued": snapshot.stats["queued"],
        "started": snapshot.stats["started"],
        "finished": snapshot.stats["finished"],
        "failed": snapshot.stats["failed"],
    }
    if disk_skipped > 0:
        status_dict["skipped (disk)"] = disk_skipped
    status_dict["total"] = total_jobs + disk_skipped
    log_summary(
        "Queue Status",
        status_dict,
        show_percentage=False,
        level="debug",
    )

    if not snapshot.running_jobs:
        return

    logger.info("Currently Running:")
    for job in snapshot.running_jobs:
        logger.info(
            "  "
            f"[{job.worker_name}] [{job.crs}] {job.benchmark}/{job.harness} "
            f"cpv={job.target_cpv_id} mode={job.mode} trial={job.trial_num} "
            f"phase={job.phase} ({job.elapsed})"
        )


def _monitor_queue_basic(
    queue,
    experiment_name: str,
    *,
    tracked_jobs: list[object] | None,
    total_jobs: int | None,
    disk_skipped: int,
    registry: "RegistryClient | None",
    callbacks: QueueMonitorCallbacks,
    poll_interval: float,
    exit_when_idle: bool,
) -> None:
    last_renew = time.monotonic()
    seen_finished: set[str] = set()
    seen_failed: set[str] = set()

    while True:
        snapshot = build_monitor_snapshot(queue, experiment_name)
        display_total = _display_total(snapshot, total_jobs)
        _log_snapshot_basic(
            snapshot,
            experiment_name=experiment_name,
            total_jobs=display_total,
            disk_skipped=disk_skipped,
        )
        completed, failed = _process_tracked_jobs(
            tracked_jobs,
            callbacks=callbacks,
            seen_finished=seen_finished,
            seen_failed=seen_failed,
        )

        if tracked_jobs:
            log_progress(
                completed + failed,
                len(tracked_jobs),
                f"Jobs complete ({completed} success, {failed} failed)",
            )
            if completed + failed >= len(tracked_jobs):
                break
        elif (
            exit_when_idle and snapshot.stats["queued"] + snapshot.stats["started"] == 0
        ):
            break

        last_renew = _renew_registry(
            registry,
            experiment_name=experiment_name,
            last_renew=last_renew,
        )
        time.sleep(poll_interval)


def _build_rich_group(
    snapshot: QueueMonitorSnapshot,
    *,
    experiment_name: str,
    total_jobs: int,
    disk_skipped: int,
):
    from rich.console import Group
    from rich.table import Table

    table = Table(title=f"Experiment: {experiment_name}")
    table.add_column("Status", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("Queued", str(snapshot.stats["queued"]))
    table.add_row("Started", str(snapshot.stats["started"]))
    table.add_row("Finished", str(snapshot.stats["finished"]), style="green")
    table.add_row("Failed", str(snapshot.stats["failed"]), style="red")
    if disk_skipped > 0:
        table.add_row("Skipped (disk)", str(disk_skipped), style="dim")
    table.add_row("Total", str(total_jobs + disk_skipped))

    running_table = Table(title="Running Jobs")
    running_table.add_column("Worker", style="green")
    running_table.add_column("CRS", style="cyan")
    running_table.add_column("Benchmark", style="yellow")
    running_table.add_column("Harness", style="yellow")
    running_table.add_column("CPV", style="magenta")
    running_table.add_column("Mode", style="blue")
    running_table.add_column("Trial", justify="right")
    running_table.add_column("Phase", style="magenta")
    running_table.add_column("Elapsed", justify="right", style="magenta")

    for job in snapshot.running_jobs:
        running_table.add_row(
            job.worker_name,
            job.crs,
            job.benchmark,
            job.harness,
            job.target_cpv_id,
            job.mode,
            job.trial_num,
            job.phase,
            job.elapsed or "N/A",
        )

    return Group(table, running_table)


def _monitor_queue_rich(
    queue,
    experiment_name: str,
    *,
    tracked_jobs: list[object] | None,
    total_jobs: int | None,
    disk_skipped: int,
    registry: "RegistryClient | None",
    callbacks: QueueMonitorCallbacks,
    poll_interval: float,
    exit_when_idle: bool,
) -> None:
    from rich.console import Console
    from rich.live import Live

    console = Console()
    last_renew = time.monotonic()
    seen_finished: set[str] = set()
    seen_failed: set[str] = set()

    snapshot = build_monitor_snapshot(queue, experiment_name)
    display_total = _display_total(snapshot, total_jobs)
    with Live(
        _build_rich_group(
            snapshot,
            experiment_name=experiment_name,
            total_jobs=display_total,
            disk_skipped=disk_skipped,
        ),
        refresh_per_second=1,
        console=console,
    ) as live:
        while True:
            completed, failed = _process_tracked_jobs(
                tracked_jobs,
                callbacks=callbacks,
                seen_finished=seen_finished,
                seen_failed=seen_failed,
            )

            if tracked_jobs and completed + failed >= len(tracked_jobs):
                break
            if (
                exit_when_idle
                and not tracked_jobs
                and snapshot.stats["queued"] + snapshot.stats["started"] == 0
            ):
                break

            last_renew = _renew_registry(
                registry,
                experiment_name=experiment_name,
                last_renew=last_renew,
            )
            snapshot = build_monitor_snapshot(queue, experiment_name)
            display_total = _display_total(snapshot, total_jobs)
            live.update(
                _build_rich_group(
                    snapshot,
                    experiment_name=experiment_name,
                    total_jobs=display_total,
                    disk_skipped=disk_skipped,
                )
            )
            time.sleep(poll_interval)
