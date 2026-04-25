"""Shared live queue monitoring for distributed orchestration commands."""

from __future__ import annotations

import importlib.util
import queue as queue_module
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, cast

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

_RICH_MONITOR_INPUT_POLL_INTERVAL_SEC = 0.1
_RICH_MONITOR_MIN_REFRESH_INTERVAL_SEC = 0.01
_RICH_MONITOR_AUTO_ROTATE_INTERVAL_SEC = 5.0
_RICH_MONITOR_POLLER_CLOSE_TIMEOUT_SEC = 0.2


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

    on_snapshot: Callable[[QueueMonitorSnapshot], None] | None = None
    on_job_finished: Callable[[object], bool | None] | None = None
    on_job_failed: Callable[[object], bool | None] | None = None


class _RichMonitorInput:
    """Best-effort non-blocking key reader for Rich monitor pagination."""

    def __init__(self, stream=None) -> None:
        self._stream = sys.stdin if stream is None else stream
        self._prefer_controlling_terminal = stream is None
        self._fd: int | None = None
        self._saved_termios_attrs: Any = None
        self._owns_fd = False
        self._active_fds: list[int] = []
        self._saved_termios_attrs_by_fd: dict[int, Any] = {}
        self._owned_fds: set[int] = set()
        self._attached_signatures: set[tuple[int, int]] = set()
        self._pending_commands: deque[str] = deque()
        self._auto_rotate_paused = False
        self.manual_navigation_available = False
        self.manual_navigation_status = (
            "n/p unavailable: stdin not interactive; auto-rotates automatically"
        )

    def _set_manual_navigation_unavailable(self, reason: str) -> "_RichMonitorInput":
        self._auto_rotate_paused = False
        self.manual_navigation_available = False
        self.manual_navigation_status = (
            f"n/p unavailable: {reason}; auto-rotates automatically"
        )
        return self

    def set_auto_rotate_paused(self, paused: bool) -> None:
        self._auto_rotate_paused = paused
        if not self.manual_navigation_available:
            return
        if paused:
            self.manual_navigation_status = "n/p active; Space resumes auto-rotate"
            return
        self.manual_navigation_status = "n/p active; Space pauses auto-rotate"

    def _sync_primary_fd(self) -> None:
        if not self._active_fds:
            self._fd = None
            self._saved_termios_attrs = None
            self._owns_fd = False
            return
        self._fd = self._active_fds[0]
        self._saved_termios_attrs = self._saved_termios_attrs_by_fd[self._fd]
        self._owns_fd = self._fd in self._owned_fds

    def _fd_signature(self, fd: int) -> tuple[int, int] | None:
        try:
            import os
            from pathlib import Path

            tty_path = os.ttyname(fd)
            stat_result = Path(tty_path).stat()
        except OSError:
            return None
        return (stat_result.st_dev, stat_result.st_ino)

    def _register_input_fd(
        self,
        fd: int,
        saved_termios_attrs: Any,
        *,
        owns_fd: bool,
    ) -> None:
        signature = self._fd_signature(fd)
        if signature is not None and signature in self._attached_signatures:
            if owns_fd:
                try:
                    import os

                    os.close(fd)
                except OSError:
                    pass
            return
        if signature is not None:
            self._attached_signatures.add(signature)
        self._active_fds.append(fd)
        self._saved_termios_attrs_by_fd[fd] = saved_termios_attrs
        if owns_fd:
            self._owned_fds.add(fd)
        self._sync_primary_fd()
        self.manual_navigation_available = True
        self.set_auto_rotate_paused(False)

    def _deactivate_input_fd(self, fd: int) -> None:
        self._active_fds = [
            active_fd for active_fd in self._active_fds if active_fd != fd
        ]
        self._sync_primary_fd()
        if self._active_fds:
            return
        self._set_manual_navigation_unavailable("hotkey input unavailable")

    def _deactivate_invalid_input_fds(self) -> bool:
        try:
            import select
        except ImportError:
            for fd in list(self._active_fds):
                self._deactivate_input_fd(fd)
            return False

        for fd in list(self._active_fds):
            try:
                select.select([fd], [], [], 0)
            except OSError:
                self._deactivate_input_fd(fd)
        return bool(self._active_fds)

    def _read_ready_input(self, fd: int, max_bytes: int) -> bytes:
        import os

        get_blocking = getattr(os, "get_blocking", None)
        set_blocking = getattr(os, "set_blocking", None)
        if not callable(get_blocking) or not callable(set_blocking):
            return os.read(fd, max_bytes)

        try:
            was_blocking = get_blocking(fd)
        except OSError:
            return os.read(fd, max_bytes)

        if not was_blocking:
            return os.read(fd, max_bytes)

        try:
            set_blocking(fd, False)
            return os.read(fd, max_bytes)
        finally:
            try:
                set_blocking(fd, True)
            except OSError:
                pass

    def _attach_stream(self, stream) -> str | None:
        isatty = getattr(stream, "isatty", None)
        fileno = getattr(stream, "fileno", None)
        if not callable(isatty):
            return "TTY state unknown"
        if not isatty():
            return "stdin not TTY"
        if not callable(fileno):
            return "no file descriptor"

        try:
            import termios
            import tty

            fd = fileno()
            saved_termios_attrs = cast("Any", termios.tcgetattr(fd))
            tty.setcbreak(fd)
        except ImportError:
            return "raw mode unsupported"
        except (OSError, ValueError, AttributeError):
            return "cbreak setup failed"

        self._register_input_fd(fd, saved_termios_attrs, owns_fd=False)
        return None

    def _attach_controlling_terminal(self) -> str | None:
        try:
            import os
            import termios
            import tty

            fd = os.open("/dev/tty", os.O_RDONLY)
        except ImportError:
            return "raw mode unsupported"
        except OSError:
            return "controlling terminal unavailable"

        try:
            saved_termios_attrs = cast("Any", termios.tcgetattr(fd))
            tty.setcbreak(fd)
        except (OSError, ValueError, AttributeError):
            os.close(fd)
            return "controlling terminal setup failed"

        self._register_input_fd(fd, saved_termios_attrs, owns_fd=True)
        return None

    def __enter__(self) -> "_RichMonitorInput":
        reasons: list[str] = []
        if self._prefer_controlling_terminal:
            controlling_terminal_reason = self._attach_controlling_terminal()
            if controlling_terminal_reason is None:
                if self._attached_signatures:
                    self._attach_stream(self._stream)
                return self
            reasons.append(controlling_terminal_reason)

        stream_reason = self._attach_stream(self._stream)
        if stream_reason is None:
            return self
        reasons.append(stream_reason)

        return self._set_manual_navigation_unavailable(" and ".join(reasons))

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._saved_termios_attrs_by_fd:
            return False

        termios_module: Any = None
        try:
            import termios as termios_module
        except ImportError:
            pass

        for fd, saved_termios_attrs in list(self._saved_termios_attrs_by_fd.items()):
            if termios_module is not None:
                try:
                    termios_module.tcsetattr(
                        fd,
                        termios_module.TCSADRAIN,
                        saved_termios_attrs,
                    )
                except (OSError, ValueError, AttributeError):
                    pass
            if fd not in self._owned_fds:
                continue
            try:
                import os

                os.close(fd)
            except OSError:
                pass
        return False

    def read_command(self, timeout_sec: float) -> str | None:
        if self._pending_commands:
            return self._pending_commands.popleft()

        if not self.manual_navigation_available or not self._active_fds:
            if timeout_sec > 0:
                time.sleep(timeout_sec)
            return None

        deadline = time.monotonic() + max(timeout_sec, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            try:
                import select

                ready, _, _ = select.select(list(self._active_fds), [], [], remaining)
                if not ready:
                    return None
                fd = ready[0]
            except OSError:
                if self._deactivate_invalid_input_fds():
                    continue
                return None

            for fd in list(ready):
                try:
                    data = self._read_ready_input(fd, 64)
                except BlockingIOError:
                    continue
                except OSError:
                    self._deactivate_input_fd(fd)
                    if not self.manual_navigation_available:
                        return None
                    continue

                if not data:
                    self._deactivate_input_fd(fd)
                    if not self.manual_navigation_available:
                        return None
                    continue

                for char in data.decode(errors="ignore"):
                    if char == " ":
                        self._pending_commands.append("space")
                        continue
                    command = char.lower()
                    if command in {"n", "p"}:
                        self._pending_commands.append(command)

                if self._pending_commands:
                    return self._pending_commands.popleft()
            if not self.manual_navigation_available:
                return None


def _rich_monitor_refresh_interval_sec(poll_interval: float) -> float:
    return (
        poll_interval if poll_interval > 0 else _RICH_MONITOR_MIN_REFRESH_INTERVAL_SEC
    )


def _rich_monitor_input_wait_sec(poll_interval: float) -> float:
    return min(
        _RICH_MONITOR_INPUT_POLL_INTERVAL_SEC,
        _rich_monitor_refresh_interval_sec(poll_interval),
    )


def _rich_monitor_auto_rotate_interval_sec(poll_interval: float) -> float:
    del poll_interval
    return _RICH_MONITOR_AUTO_ROTATE_INTERVAL_SEC


class _RichMonitorSnapshotPoller:
    """Refresh snapshots off the UI thread so local key handling stays responsive."""

    def __init__(
        self,
        queue,
        experiment_name: str,
        *,
        registry: "RegistryClient | None",
        poll_interval: float,
    ) -> None:
        self._queue = queue
        self._experiment_name = experiment_name
        self._registry = registry
        self._refresh_interval_sec = _rich_monitor_refresh_interval_sec(poll_interval)
        self._last_renew = time.monotonic()
        self._updates: queue_module.Queue[QueueMonitorSnapshot | Exception] = (
            queue_module.Queue(maxsize=1)
        )
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="crsbench-rich-monitor-refresh",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=_RICH_MONITOR_POLLER_CLOSE_TIMEOUT_SEC)
        if self._thread.is_alive():
            logger.warning("Rich monitor refresh is still stopping in the background")

    def drain_latest(self) -> QueueMonitorSnapshot | None:
        latest_snapshot: QueueMonitorSnapshot | None = None
        while True:
            try:
                item = self._updates.get_nowait()
            except queue_module.Empty:
                break
            if isinstance(item, Exception):
                raise item
            latest_snapshot = item
        return latest_snapshot

    def _publish(self, item: QueueMonitorSnapshot | Exception) -> None:
        while True:
            try:
                self._updates.put_nowait(item)
                return
            except queue_module.Full:
                try:
                    self._updates.get_nowait()
                except queue_module.Empty:
                    continue

    def _run(self) -> None:
        try:
            while not self._stop_event.wait(self._refresh_interval_sec):
                self._last_renew = _renew_registry(
                    self._registry,
                    experiment_name=self._experiment_name,
                    last_renew=self._last_renew,
                )
                snapshot = build_monitor_snapshot(self._queue, self._experiment_name)
                self._publish(snapshot)
        except Exception as exc:
            self._publish(exc)


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


def _display_worker_name(experiment_name: str, worker_name: str) -> str:
    prefix = f"crsbench-{experiment_name}-"
    if worker_name.startswith(prefix):
        return worker_name.removeprefix(prefix)
    return worker_name


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


def _notify_snapshot(
    callbacks: QueueMonitorCallbacks,
    snapshot: QueueMonitorSnapshot,
) -> None:
    if callbacks.on_snapshot is not None:
        callbacks.on_snapshot(snapshot)


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
            f"[{_display_worker_name(experiment_name, job.worker_name)}] "
            f"[{job.crs}] {job.benchmark}/{job.harness} "
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
        _notify_snapshot(callbacks, snapshot)
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
    running_jobs: list[RunningJobInfo] | None = None,
    running_job_count: int | None = None,
    paging_active: bool = False,
    page_index: int = 0,
    page_count: int = 1,
    paging_status_text: str | None = None,
    auto_rotate_paused: bool = False,
):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    visible_running_jobs = (
        snapshot.running_jobs if running_jobs is None else running_jobs
    )
    total_running_jobs = (
        len(snapshot.running_jobs) if running_job_count is None else running_job_count
    )

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
    footer = None
    if paging_active and total_running_jobs > len(visible_running_jobs):
        helper_text = paging_status_text or "auto-rotates automatically"
        page_text = (
            f"Page {page_index + 1}/{page_count}: "
            f"showing {len(visible_running_jobs)} of {total_running_jobs} running jobs"
        )
        helper_footer = Table.grid(expand=True, padding=(0, 0))
        helper_footer.add_column(ratio=1)
        helper_footer.add_column(
            justify="right",
            width=len(" [PAUSED]"),
            no_wrap=True,
        )
        helper_footer.add_row(
            Text(
                helper_text,
                style="dim italic",
                no_wrap=True,
                overflow="ellipsis",
            ),
            Text(
                " [PAUSED]" if auto_rotate_paused else "",
                style="bold red",
                no_wrap=True,
            ),
        )
        footer = Group(
            Text(
                page_text,
                style="dim italic",
                no_wrap=True,
                overflow="ellipsis",
            ),
            helper_footer,
        )
    running_table.add_column("Worker", style="green")
    running_table.add_column("CRS", style="cyan")
    running_table.add_column("Benchmark", style="yellow")
    running_table.add_column("Harness", style="yellow")
    running_table.add_column("CPV", style="magenta")
    running_table.add_column("Mode", style="blue")
    running_table.add_column("Trial", justify="right")
    running_table.add_column("Phase", style="magenta")
    running_table.add_column("Elapsed", justify="right", style="magenta")

    for job in visible_running_jobs:
        running_table.add_row(
            _display_worker_name(experiment_name, job.worker_name),
            job.crs,
            job.benchmark,
            job.harness,
            job.target_cpv_id,
            job.mode,
            job.trial_num,
            job.phase,
            job.elapsed or "N/A",
        )

    if footer is not None:
        return Group(table, running_table, footer)
    return Group(table, running_table)


def _count_rendered_lines(console, renderable) -> int:
    options = console.options.copy()
    options.height = None
    options.max_height = 10_000
    return len(console.render_lines(renderable, options=options, pad=False))


def _select_running_jobs_window(
    console,
    snapshot: QueueMonitorSnapshot,
    *,
    experiment_name: str,
    total_jobs: int,
    disk_skipped: int,
    page_index: int,
    paging_status_text: str | None = None,
    auto_rotate_paused: bool = False,
) -> tuple[list[RunningJobInfo], bool, int, int]:
    running_jobs = snapshot.running_jobs
    total_running_jobs = len(running_jobs)
    if total_running_jobs <= 1:
        return running_jobs, False, 0, 1

    full_group = _build_rich_group(
        snapshot,
        experiment_name=experiment_name,
        total_jobs=total_jobs,
        disk_skipped=disk_skipped,
    )
    if _count_rendered_lines(console, full_group) <= console.size.height:
        return running_jobs, False, 0, 1

    low = 0
    high = total_running_jobs
    best = 0

    while low <= high:
        candidate_count = (low + high) // 2
        candidate_jobs = running_jobs[:candidate_count]
        candidate_group = _build_rich_group(
            snapshot,
            experiment_name=experiment_name,
            total_jobs=total_jobs,
            disk_skipped=disk_skipped,
            running_jobs=candidate_jobs,
            running_job_count=total_running_jobs,
            paging_active=True,
            page_index=0,
            page_count=1,
            paging_status_text=paging_status_text,
            auto_rotate_paused=auto_rotate_paused,
        )
        if _count_rendered_lines(console, candidate_group) <= console.size.height:
            best = candidate_count
            low = candidate_count + 1
        else:
            high = candidate_count - 1

    if best <= 0:
        return [], True, 0, 1

    page_count = (total_running_jobs + best - 1) // best
    selected_page_index = min(max(page_index, 0), page_count - 1)
    start = selected_page_index * best
    end = min(start + best, total_running_jobs)
    visible_jobs = running_jobs[start:end]
    return visible_jobs, True, selected_page_index, page_count


def _page_navigation_idle_timeout_sec(poll_interval: float) -> float:
    """Keep manual page selection visible briefly before auto-rotation resumes."""
    return _rich_monitor_auto_rotate_interval_sec(poll_interval)


def _should_auto_rotate_pages(
    *,
    last_manual_page_change_at: float | None,
    now: float,
    poll_interval: float,
) -> bool:
    """Return True when the Rich monitor should resume automatic page rotation."""
    if last_manual_page_change_at is None:
        return True
    return now - last_manual_page_change_at >= _page_navigation_idle_timeout_sec(
        poll_interval
    )


def _apply_page_navigation_command(
    *,
    command: str,
    page_index: int,
    page_count: int,
) -> int:
    """Move one running-jobs page forward or backward."""
    if page_count <= 1:
        return 0
    if command == "n":
        return (page_index + 1) % page_count
    if command == "p":
        return (page_index - 1) % page_count
    return page_index


def _build_rich_renderable(
    console,
    snapshot: QueueMonitorSnapshot,
    *,
    experiment_name: str,
    total_jobs: int,
    disk_skipped: int,
    page_index: int,
    paging_status_text: str,
    auto_rotate_paused: bool = False,
):
    visible_running_jobs, paging_active, selected_page_index, page_count = (
        _select_running_jobs_window(
            console,
            snapshot,
            experiment_name=experiment_name,
            total_jobs=total_jobs,
            disk_skipped=disk_skipped,
            page_index=page_index,
            paging_status_text=paging_status_text,
            auto_rotate_paused=auto_rotate_paused,
        )
    )
    renderable = _build_rich_group(
        snapshot,
        experiment_name=experiment_name,
        total_jobs=total_jobs,
        disk_skipped=disk_skipped,
        running_jobs=visible_running_jobs,
        running_job_count=len(snapshot.running_jobs),
        paging_active=paging_active,
        page_index=selected_page_index,
        page_count=page_count,
        paging_status_text=paging_status_text,
        auto_rotate_paused=auto_rotate_paused,
    )
    return renderable, selected_page_index, page_count, paging_active


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
    seen_finished: set[str] = set()
    seen_failed: set[str] = set()
    page_index = 0
    last_manual_page_change_at: float | None = None
    auto_rotate_paused = False

    snapshot = build_monitor_snapshot(queue, experiment_name)
    _notify_snapshot(callbacks, snapshot)
    display_total = _display_total(snapshot, total_jobs)
    with _RichMonitorInput() as monitor_input:
        (
            renderable,
            page_index,
            page_count,
            paging_active,
        ) = _build_rich_renderable(
            console,
            snapshot,
            experiment_name=experiment_name,
            total_jobs=display_total,
            disk_skipped=disk_skipped,
            page_index=page_index,
            paging_status_text=monitor_input.manual_navigation_status,
            auto_rotate_paused=auto_rotate_paused,
        )
        with Live(
            renderable,
            refresh_per_second=1,
            console=console,
        ) as live:
            poller: _RichMonitorSnapshotPoller | None = None
            refresh_interval_sec = _rich_monitor_refresh_interval_sec(poll_interval)
            input_wait_sec = _rich_monitor_input_wait_sec(poll_interval)
            auto_rotate_interval_sec = _rich_monitor_auto_rotate_interval_sec(
                poll_interval
            )
            tracked_job_refresh_at = time.monotonic()
            completed = 0
            failed = 0

            def _render_current_snapshot(*, refresh: bool) -> None:
                nonlocal renderable, page_index, page_count, paging_active
                (
                    renderable,
                    page_index,
                    page_count,
                    paging_active,
                ) = _build_rich_renderable(
                    console,
                    snapshot,
                    experiment_name=experiment_name,
                    total_jobs=display_total,
                    disk_skipped=disk_skipped,
                    page_index=page_index,
                    paging_status_text=monitor_input.manual_navigation_status,
                    auto_rotate_paused=auto_rotate_paused,
                )
                live.update(renderable, refresh=refresh)

            next_auto_rotate_at = time.monotonic() + auto_rotate_interval_sec
            try:
                while True:
                    if poller is not None:
                        latest_snapshot = poller.drain_latest()
                        if latest_snapshot is not None:
                            was_paging_active = paging_active
                            was_page_count = page_count
                            snapshot = latest_snapshot
                            _notify_snapshot(callbacks, snapshot)
                            display_total = _display_total(snapshot, total_jobs)
                            _render_current_snapshot(refresh=True)
                            if paging_active and (
                                not was_paging_active or page_count != was_page_count
                            ):
                                next_auto_rotate_at = (
                                    time.monotonic() + auto_rotate_interval_sec
                                )

                    now = time.monotonic()
                    if tracked_jobs and now >= tracked_job_refresh_at:
                        completed, failed = _process_tracked_jobs(
                            tracked_jobs,
                            callbacks=callbacks,
                            seen_finished=seen_finished,
                            seen_failed=seen_failed,
                        )
                        tracked_job_refresh_at = now + refresh_interval_sec

                    if tracked_jobs and completed + failed >= len(tracked_jobs):
                        break
                    if (
                        exit_when_idle
                        and not tracked_jobs
                        and snapshot.stats["queued"] + snapshot.stats["started"] == 0
                    ):
                        break

                    if poller is None:
                        poller = _RichMonitorSnapshotPoller(
                            queue,
                            experiment_name,
                            registry=registry,
                            poll_interval=poll_interval,
                        )
                        poller.start()

                    if (
                        paging_active
                        and not auto_rotate_paused
                        and now >= next_auto_rotate_at
                        and _should_auto_rotate_pages(
                            last_manual_page_change_at=last_manual_page_change_at,
                            now=now,
                            poll_interval=poll_interval,
                        )
                    ):
                        page_index = (page_index + 1) % page_count
                        next_auto_rotate_at = now + auto_rotate_interval_sec
                        _render_current_snapshot(refresh=True)

                    command = monitor_input.read_command(input_wait_sec)
                    if command is None:
                        continue
                    if command == "space":
                        auto_rotate_paused = not auto_rotate_paused
                        monitor_input.set_auto_rotate_paused(auto_rotate_paused)
                        if not auto_rotate_paused:
                            next_auto_rotate_at = (
                                time.monotonic() + auto_rotate_interval_sec
                            )
                        _render_current_snapshot(refresh=True)
                        continue
                    if not paging_active:
                        continue
                    page_index = _apply_page_navigation_command(
                        command=command,
                        page_index=page_index,
                        page_count=page_count,
                    )
                    last_manual_page_change_at = time.monotonic()
                    next_auto_rotate_at = (
                        last_manual_page_change_at + auto_rotate_interval_sec
                    )
                    _render_current_snapshot(refresh=True)
            finally:
                if poller is not None:
                    poller.close()
