"""Tests for the shared distributed queue monitor."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from crsbench.distributed.queue_monitor import (
    QueueJobEntry,
    QueueMonitorCallbacks,
    QueueMonitorSnapshot,
    RunningJobInfo,
    _apply_page_navigation_command,
    _build_rich_group,
    _display_worker_name,
    _page_navigation_idle_timeout_sec,
    _RichMonitorInput,
    _select_running_jobs_window,
    _should_auto_rotate_pages,
    build_monitor_snapshot,
    list_queue_job_entries,
    monitor_queue,
)


def test_build_monitor_snapshot_uses_experiment_scoped_counts() -> None:
    queue = MagicMock()
    started_job = MagicMock()
    started_job.meta = {
        "worker_name": "worker-1",
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness": "fuzz-a",
        "target_cpv_id": "cpv-1",
        "mode": "delta",
        "trial_num": 1,
        "phase": "running",
    }

    with (
        patch(
            "crsbench.distributed.queue_monitor.get_queue_stats",
            return_value={
                "queued": 99,
                "started": 44,
                "finished": 12,
                "failed": 9,
                "workers": 7,
            },
        ),
        patch(
            "crsbench.distributed.queue_monitor.get_existing_trial_jobs",
            return_value={
                "queued": [MagicMock(), MagicMock()],
                "started": [started_job],
                "finished": [MagicMock() for _ in range(3)],
                "failed": [MagicMock()],
                "deferred": [],
                "scheduled": [],
            },
        ),
    ):
        snapshot = build_monitor_snapshot(queue, "exp-1")

    assert snapshot.stats == {
        "queued": 2,
        "started": 1,
        "finished": 3,
        "failed": 1,
        "workers": 7,
    }
    assert len(snapshot.running_jobs) == 1
    assert snapshot.running_jobs[0].worker_name == "worker-1"
    assert snapshot.running_jobs[0].phase == "running"


def test_list_queue_job_entries_maps_registry_states_to_status_rows() -> None:
    queue = MagicMock()
    queued_job = MagicMock()
    queued_job.id = "job-queued"
    queued_job.meta = {"retry_count": 0}
    started_job = MagicMock()
    started_job.id = "job-started"
    started_job.meta = {"worker_name": "worker-1", "retry_count": 2}
    failed_job = MagicMock()
    failed_job.id = "job-failed"
    failed_job.meta = {"retry_count": 3}

    with (
        patch(
            "crsbench.distributed.queue_monitor.get_existing_trial_jobs",
            return_value={
                "queued": [queued_job],
                "started": [started_job],
                "finished": [],
                "failed": [failed_job],
                "deferred": [],
                "scheduled": [],
            },
        ),
        patch(
            "crsbench.distributed.queue_monitor.get_trial_key",
            side_effect=lambda job: f"trial:{job.id}",
        ),
    ):
        entries = list_queue_job_entries(queue, "exp-1")

    assert entries == [
        QueueJobEntry(
            job_id="job-failed",
            trial_key="trial:job-failed",
            state="failed",
            claimed_by=None,
            retry_count=3,
        ),
        QueueJobEntry(
            job_id="job-queued",
            trial_key="trial:job-queued",
            state="queued",
            claimed_by=None,
            retry_count=0,
        ),
        QueueJobEntry(
            job_id="job-started",
            trial_key="trial:job-started",
            state="running",
            claimed_by="worker-1",
            retry_count=2,
        ),
    ]


def test_list_queue_job_entries_preserves_duplicate_physical_jobs() -> None:
    queue = MagicMock()
    job_a = MagicMock()
    job_a.id = "job-a"
    job_a.meta = {"retry_count": 0}
    job_b = MagicMock()
    job_b.id = "job-b"
    job_b.meta = {"retry_count": 1}

    with (
        patch(
            "crsbench.distributed.queue_monitor.get_existing_trial_jobs",
            return_value={
                "queued": [job_a, job_b],
                "started": [],
                "finished": [],
                "failed": [],
                "deferred": [],
                "scheduled": [],
            },
        ),
        patch(
            "crsbench.distributed.queue_monitor.get_trial_key",
            return_value="trial:duplicate",
        ),
    ):
        entries = list_queue_job_entries(queue, "exp-1")

    assert entries == [
        QueueJobEntry(
            job_id="job-a",
            trial_key="trial:duplicate",
            state="queued",
            claimed_by=None,
            retry_count=0,
        ),
        QueueJobEntry(
            job_id="job-b",
            trial_key="trial:duplicate",
            state="queued",
            claimed_by=None,
            retry_count=1,
        ),
    ]


def test_list_queue_job_entries_ignores_stale_worker_name_outside_running() -> None:
    queue = MagicMock()
    queued_job = MagicMock()
    queued_job.id = "job-queued"
    queued_job.meta = {"worker_name": "worker-stale", "retry_count": 1}
    failed_job = MagicMock()
    failed_job.id = "job-failed"
    failed_job.meta = {"worker_name": "worker-stale", "retry_count": 2}
    started_job = MagicMock()
    started_job.id = "job-started"
    started_job.meta = {"worker_name": "worker-live", "retry_count": 0}

    with (
        patch(
            "crsbench.distributed.queue_monitor.get_existing_trial_jobs",
            return_value={
                "queued": [queued_job],
                "started": [started_job],
                "finished": [],
                "failed": [failed_job],
                "deferred": [],
                "scheduled": [],
            },
        ),
        patch(
            "crsbench.distributed.queue_monitor.get_trial_key",
            side_effect=lambda job: f"trial:{job.id}",
        ),
    ):
        entries = list_queue_job_entries(queue, "exp-1")

    assert entries == [
        QueueJobEntry(
            job_id="job-failed",
            trial_key="trial:job-failed",
            state="failed",
            claimed_by=None,
            retry_count=2,
        ),
        QueueJobEntry(
            job_id="job-queued",
            trial_key="trial:job-queued",
            state="queued",
            claimed_by=None,
            retry_count=1,
        ),
        QueueJobEntry(
            job_id="job-started",
            trial_key="trial:job-started",
            state="running",
            claimed_by="worker-live",
            retry_count=0,
        ),
    ]


def test_monitor_queue_attach_mode_is_read_only() -> None:
    queue = MagicMock()
    callbacks = QueueMonitorCallbacks(
        on_job_finished=MagicMock(),
        on_job_failed=MagicMock(),
    )

    active = QueueMonitorSnapshot(
        stats={"queued": 1, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 1, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=callbacks,
            use_rich=False,
            poll_interval=0,
        )

    callbacks.on_job_finished.assert_not_called()
    callbacks.on_job_failed.assert_not_called()


def test_monitor_queue_calls_snapshot_callback_in_basic_mode() -> None:
    queue = MagicMock()
    active = QueueMonitorSnapshot(
        stats={"queued": 1, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    snapshots: list[QueueMonitorSnapshot] = []

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(on_snapshot=snapshots.append),
            use_rich=False,
            poll_interval=0,
        )

    assert snapshots == [active, done]


def test_monitor_queue_calls_snapshot_callback_in_rich_mode() -> None:
    queue = MagicMock()
    active = QueueMonitorSnapshot(
        stats={"queued": 1, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    snapshots: list[QueueMonitorSnapshot] = []

    class DummyLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *args, **kwargs):
            return None

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("rich.live.Live", DummyLive),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(on_snapshot=snapshots.append),
            use_rich=True,
            poll_interval=0,
        )

    assert snapshots == [active, done]


def test_select_running_jobs_window_pages_when_terminal_height_is_limited() -> None:
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(width=120, height=20, force_terminal=True)
    running_jobs = [
        RunningJobInfo(
            worker_name=f"worker-{idx}",
            crs="crs-a",
            benchmark="bench-a",
            harness="harness-a",
            target_cpv_id="cpv-1",
            mode="delta",
            trial_num=str(idx),
            phase="running",
            elapsed="1m0s",
        )
        for idx in range(6)
    ]
    snapshot = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=running_jobs,
    )

    visible_jobs, paging_active, selected_page_index, page_count = (
        _select_running_jobs_window(
            console,
            snapshot,
            experiment_name="exp-1",
            total_jobs=6,
            disk_skipped=0,
            page_index=0,
            paging_status_text="n/p active; auto-rotates when idle",
        )
    )

    assert paging_active is True
    assert [job.trial_num for job in visible_jobs] == ["0", "1", "2", "3"]
    assert (selected_page_index, page_count) == (0, 2)

    wrapped_jobs, wrapped_paging_active, wrapped_page_index, wrapped_page_count = (
        _select_running_jobs_window(
            console,
            snapshot,
            experiment_name="exp-1",
            total_jobs=6,
            disk_skipped=0,
            page_index=1,
            paging_status_text="n/p active; auto-rotates when idle",
        )
    )

    assert wrapped_paging_active is True
    assert [job.trial_num for job in wrapped_jobs] == ["4", "5"]
    assert (wrapped_page_index, wrapped_page_count) == (1, 2)


def test_select_running_jobs_window_clamps_page_index_when_count_shrinks() -> None:
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(width=120, height=20, force_terminal=True)
    running_jobs = [
        RunningJobInfo(
            worker_name=f"worker-{idx}",
            crs="crs-a",
            benchmark="bench-a",
            harness="harness-a",
            target_cpv_id="cpv-1",
            mode="delta",
            trial_num=str(idx),
            phase="running",
            elapsed="1m0s",
        )
        for idx in range(6)
    ]
    snapshot = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=running_jobs,
    )

    visible_jobs, paging_active, selected_page_index, page_count = (
        _select_running_jobs_window(
            console,
            snapshot,
            experiment_name="exp-1",
            total_jobs=6,
            disk_skipped=0,
            page_index=5,
            paging_status_text="n/p active; auto-rotates when idle",
        )
    )

    assert paging_active is True
    assert [job.trial_num for job in visible_jobs] == ["4", "5"]
    assert (selected_page_index, page_count) == (1, 2)


def test_apply_page_navigation_command_wraps_both_directions() -> None:
    assert _apply_page_navigation_command(command="n", page_index=1, page_count=2) == 0
    assert _apply_page_navigation_command(command="p", page_index=0, page_count=2) == 1
    assert _apply_page_navigation_command(command="x", page_index=0, page_count=2) == 0


def test_should_auto_rotate_pages_respects_idle_timeout() -> None:
    idle_timeout_sec = _page_navigation_idle_timeout_sec(3.0)

    assert _should_auto_rotate_pages(
        last_manual_page_change_at=None,
        now=10.0,
        poll_interval=3.0,
    )
    assert not _should_auto_rotate_pages(
        last_manual_page_change_at=10.0,
        now=10.0 + idle_timeout_sec - 0.1,
        poll_interval=3.0,
    )
    assert _should_auto_rotate_pages(
        last_manual_page_change_at=10.0,
        now=10.0 + idle_timeout_sec,
        poll_interval=3.0,
    )


def test_select_running_jobs_window_shows_all_rows_when_terminal_can_fit_them() -> None:
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(width=120, height=21, force_terminal=True)
    running_jobs = [
        RunningJobInfo(
            worker_name=f"worker-{idx}",
            crs="crs-a",
            benchmark="bench-a",
            harness="harness-a",
            target_cpv_id="cpv-1",
            mode="delta",
            trial_num=str(idx),
            phase="running",
            elapsed="1m0s",
        )
        for idx in range(6)
    ]
    snapshot = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=running_jobs,
    )

    visible_jobs, paging_active, page_index, page_count = _select_running_jobs_window(
        console,
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        page_index=3,
        paging_status_text="n/p active; auto-rotates when idle",
    )

    assert paging_active is False
    assert [job.trial_num for job in visible_jobs] == ["0", "1", "2", "3", "4", "5"]
    assert (page_index, page_count) == (0, 1)


def test_build_rich_group_caption_shows_page_indicator_and_key_help() -> None:
    rich_console = pytest.importorskip("rich.console")
    snapshot = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[
            RunningJobInfo(
                worker_name=f"worker-{idx}",
                crs="crs-a",
                benchmark="bench-a",
                harness="harness-a",
                target_cpv_id="cpv-1",
                mode="delta",
                trial_num=str(idx),
                phase="running",
                elapsed="1m0s",
            )
            for idx in range(6)
        ],
    )

    renderable = _build_rich_group(
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        running_jobs=snapshot.running_jobs[:4],
        running_job_count=6,
        paging_active=True,
        page_index=1,
        page_count=2,
        paging_status_text="n/p active; auto-rotates when idle",
    )
    console = rich_console.Console(width=120, force_terminal=True, record=True)
    console.print(renderable)

    output = console.export_text()
    assert "Page 2/2: showing 4 of 6 running jobs;" in output
    assert "n/p active; auto-rotates when idle" in output


def test_build_rich_group_caption_reports_hotkeys_unavailable_reason() -> None:
    rich_console = pytest.importorskip("rich.console")
    snapshot = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[
            RunningJobInfo(
                worker_name=f"worker-{idx}",
                crs="crs-a",
                benchmark="bench-a",
                harness="harness-a",
                target_cpv_id="cpv-1",
                mode="delta",
                trial_num=str(idx),
                phase="running",
                elapsed="1m0s",
            )
            for idx in range(6)
        ],
    )

    renderable = _build_rich_group(
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        running_jobs=snapshot.running_jobs[:4],
        running_job_count=6,
        paging_active=True,
        page_index=0,
        page_count=2,
        paging_status_text="n/p unavailable: stdin not TTY; auto-rotates each refresh",
    )
    console = rich_console.Console(width=120, force_terminal=True, record=True)
    console.print(renderable)

    output = console.export_text()
    assert "Page 1/2: showing 4 of 6 running jobs;" in output
    assert "n/p unavailable: stdin not TTY;" in output
    assert "auto-rotates each" in output
    assert "refresh" in output


def test_rich_monitor_input_reports_non_tty_unavailability_reason() -> None:
    stream = MagicMock()
    stream.isatty.return_value = False

    with _RichMonitorInput(stream) as monitor_input:
        assert monitor_input.manual_navigation_available is False
        assert monitor_input.manual_navigation_status == (
            "n/p unavailable: stdin not TTY; auto-rotates each refresh"
        )


def test_rich_monitor_input_prefers_controlling_terminal_when_available() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11) as mock_open,
        patch("os.close") as mock_close,
        patch("termios.tcgetattr", return_value=["saved-attrs"]) as mock_tcgetattr,
        patch("termios.tcsetattr") as mock_tcsetattr,
        patch("tty.setcbreak") as mock_setcbreak,
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert (
            monitor_input.manual_navigation_status
            == "n/p active; auto-rotates when idle"
        )
        assert monitor_input._fd == 11
        stream.fileno.assert_not_called()
        mock_open.assert_called_once_with("/dev/tty", os.O_RDONLY)
        mock_tcgetattr.assert_called_once_with(11)
        mock_setcbreak.assert_called_once_with(11)

    mock_tcsetattr.assert_called_once()
    mock_close.assert_called_once_with(11)


def test_rich_monitor_input_falls_back_to_stdin_when_tty_open_fails() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", side_effect=OSError("no controlling terminal")),
        patch("termios.tcgetattr", return_value=["saved-attrs"]) as mock_tcgetattr,
        patch("termios.tcsetattr") as mock_tcsetattr,
        patch("tty.setcbreak") as mock_setcbreak,
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert (
            monitor_input.manual_navigation_status
            == "n/p active; auto-rotates when idle"
        )
        assert monitor_input._fd == 7
        stream.fileno.assert_called_once_with()
        mock_tcgetattr.assert_called_once_with(7)
        mock_setcbreak.assert_called_once_with(7)

    mock_tcsetattr.assert_called_once()


def test_rich_monitor_input_reads_from_distinct_stdin_when_tty_is_silent() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    def _fake_ttyname(fd: int) -> str:
        if fd == 11:
            return "/dev/pts/11"
        if fd == 7:
            return "/dev/pts/7"
        raise AssertionError(f"unexpected fd {fd}")

    def _fake_stat(path, **_kwargs) -> SimpleNamespace:
        path_str = str(path)
        if path_str == "/dev/pts/11":
            return SimpleNamespace(st_dev=1, st_ino=11)
        if path_str == "/dev/pts/7":
            return SimpleNamespace(st_dev=2, st_ino=7)
        raise AssertionError(f"unexpected tty path {path_str}")

    def _fake_select(readers, _writers, _errors, _timeout):
        return ([7] if 7 in readers else [], [], [])

    def _fake_read(fd: int, _size: int) -> bytes:
        if fd != 7:
            raise AssertionError(f"unexpected read fd {fd}")
        return b"n"

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11),
        patch("os.close") as mock_close,
        patch("os.ttyname", side_effect=_fake_ttyname),
        patch("os.stat", side_effect=_fake_stat),
        patch("termios.tcgetattr", return_value=["saved-attrs"]) as mock_tcgetattr,
        patch("termios.tcsetattr") as mock_tcsetattr,
        patch("tty.setcbreak") as mock_setcbreak,
        patch("select.select", side_effect=_fake_select),
        patch("os.read", side_effect=_fake_read),
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert monitor_input.read_command(0.1) == "n"

    assert mock_tcgetattr.call_args_list == [call(11), call(7)]
    assert mock_setcbreak.call_args_list == [call(11), call(7)]
    assert mock_tcsetattr.call_count == 2
    mock_close.assert_called_once_with(11)


def test_rich_monitor_input_disables_navigation_after_all_input_sources_fail() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    def _fake_ttyname(fd: int) -> str:
        return f"/dev/pts/{fd}"

    def _fake_stat(path, **_kwargs) -> SimpleNamespace:
        fd = int(str(path).rsplit("/", maxsplit=1)[-1])
        return SimpleNamespace(st_dev=fd, st_ino=fd)

    def _fake_read(fd: int, _size: int) -> bytes:
        if fd == 11:
            return b""
        if fd == 7:
            raise OSError("stdin read failed")
        raise AssertionError(f"unexpected read fd {fd}")

    def _fake_select(readers, _writers, _errors, _timeout):
        if readers == [11, 7]:
            return ([11, 7], [], [])
        if readers == [7]:
            return ([7], [], [])
        raise AssertionError(f"unexpected readers {readers}")

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11),
        patch("os.close") as mock_close,
        patch("os.ttyname", side_effect=_fake_ttyname),
        patch("os.stat", side_effect=_fake_stat),
        patch("termios.tcgetattr", return_value=["saved-attrs"]),
        patch("termios.tcsetattr"),
        patch("tty.setcbreak"),
        patch("select.select", side_effect=_fake_select),
        patch("os.read", side_effect=_fake_read),
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert monitor_input.read_command(0.1) is None
        assert monitor_input.manual_navigation_available is False
        assert monitor_input.manual_navigation_status == (
            "n/p unavailable: hotkey input unavailable; auto-rotates each refresh"
        )

    mock_close.assert_called_once_with(11)


def test_rich_monitor_input_recovers_when_select_fails_for_only_one_source() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    def _fake_ttyname(fd: int) -> str:
        return f"/dev/pts/{fd}"

    def _fake_stat(path, **_kwargs) -> SimpleNamespace:
        fd = int(str(path).rsplit("/", maxsplit=1)[-1])
        return SimpleNamespace(st_dev=fd, st_ino=fd)

    def _fake_select(readers, _writers, _errors, _timeout):
        if readers == [11, 7]:
            raise OSError("bad fd in multi-select")
        if readers == [11]:
            raise OSError("controlling tty is stale")
        if readers == [7]:
            return ([7], [], [])
        raise AssertionError(f"unexpected readers {readers}")

    def _fake_read(fd: int, _size: int) -> bytes:
        if fd != 7:
            raise AssertionError(f"unexpected read fd {fd}")
        return b"n"

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11),
        patch("os.close") as mock_close,
        patch("os.ttyname", side_effect=_fake_ttyname),
        patch("os.stat", side_effect=_fake_stat),
        patch("termios.tcgetattr", return_value=["saved-attrs"]),
        patch("termios.tcsetattr"),
        patch("tty.setcbreak"),
        patch("select.select", side_effect=_fake_select),
        patch("os.read", side_effect=_fake_read),
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert monitor_input.read_command(0.1) == "n"
        assert monitor_input.manual_navigation_available is True

    mock_close.assert_called_once_with(11)


def test_rich_monitor_input_stops_after_first_ready_fd_yields_a_command() -> None:
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    def _fake_ttyname(fd: int) -> str:
        if fd == 11:
            return "/dev/tty"
        if fd == 7:
            return "/dev/pts/7"
        raise AssertionError(f"unexpected fd {fd}")

    def _fake_stat(path, **_kwargs) -> SimpleNamespace:
        path_str = str(path)
        if path_str == "/dev/tty":
            return SimpleNamespace(st_dev=1, st_ino=11)
        if path_str == "/dev/pts/7":
            return SimpleNamespace(st_dev=2, st_ino=7)
        raise AssertionError(f"unexpected tty path {path_str}")

    def _fake_select(readers, _writers, _errors, _timeout):
        if readers == [11, 7]:
            return ([11, 7], [], [])
        raise AssertionError(f"unexpected readers {readers}")

    def _fake_read(fd: int, _size: int) -> bytes:
        if fd == 11:
            return b"n"
        raise AssertionError("second ready fd should not be read after a command")

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11),
        patch("os.close") as mock_close,
        patch("os.ttyname", side_effect=_fake_ttyname),
        patch("os.stat", side_effect=_fake_stat),
        patch("termios.tcgetattr", return_value=["saved-attrs"]),
        patch("termios.tcsetattr"),
        patch("tty.setcbreak"),
        patch("select.select", side_effect=_fake_select),
        patch("os.read", side_effect=_fake_read),
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert monitor_input.read_command(0.1) == "n"

    mock_close.assert_called_once_with(11)


def test_rich_monitor_input_reads_later_ready_fd_when_first_has_non_command_data() -> (
    None
):
    stream = MagicMock()
    stream.isatty.return_value = True
    stream.fileno.return_value = 7

    def _fake_ttyname(fd: int) -> str:
        if fd == 11:
            return "/dev/tty"
        if fd == 7:
            return "/dev/pts/7"
        raise AssertionError(f"unexpected fd {fd}")

    def _fake_stat(path, **_kwargs) -> SimpleNamespace:
        path_str = str(path)
        if path_str == "/dev/tty":
            return SimpleNamespace(st_dev=1, st_ino=11)
        if path_str == "/dev/pts/7":
            return SimpleNamespace(st_dev=2, st_ino=7)
        raise AssertionError(f"unexpected tty path {path_str}")

    select_calls = {"count": 0}

    def _fake_select(readers, _writers, _errors, _timeout):
        select_calls["count"] += 1
        if select_calls["count"] == 1 and readers == [11, 7]:
            return ([11, 7], [], [])
        return ([], [], [])

    def _fake_read(fd: int, _size: int) -> bytes:
        if fd == 11:
            return b"x"
        if fd == 7:
            return b"n"
        raise AssertionError(f"unexpected read fd {fd}")

    with (
        patch("crsbench.distributed.queue_monitor.sys.stdin", stream),
        patch("os.open", return_value=11) as _mock_open,
        patch("os.close") as mock_close,
        patch("os.ttyname", side_effect=_fake_ttyname),
        patch("os.stat", side_effect=_fake_stat),
        patch("termios.tcgetattr", return_value=["saved-attrs"]),
        patch("termios.tcsetattr"),
        patch("tty.setcbreak"),
        patch("select.select", side_effect=_fake_select),
        patch("os.read", side_effect=_fake_read),
        _RichMonitorInput() as monitor_input,
    ):
        assert monitor_input.manual_navigation_available is True
        assert monitor_input.read_command(0.1) == "n"

    mock_close.assert_called_once_with(11)


def test_monitor_queue_rich_applies_manual_page_navigation_immediately() -> None:
    rich_console = pytest.importorskip("rich.console")
    queue = MagicMock()
    active = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 6, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[
            RunningJobInfo(
                worker_name=f"worker-{idx}",
                crs="crs-a",
                benchmark="bench-a",
                harness="harness-a",
                target_cpv_id="cpv-1",
                mode="delta",
                trial_num=str(idx),
                phase="running",
                elapsed="1m0s",
            )
            for idx in range(6)
        ],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 6, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    rendered_updates = []
    refresh_flags = []

    class DummyLive:
        def __init__(self, renderable, *args, **kwargs):
            del args, kwargs
            rendered_updates.append(renderable)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, renderable, *args, **kwargs):
            rendered_updates.append(renderable)
            refresh_flags.append(kwargs.get("refresh"))

    class DummyInput:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.manual_navigation_available = True
            self.manual_navigation_status = "n/p active; auto-rotates when idle"
            self._commands = iter(["n", None])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read_command(self, timeout_sec: float) -> str | None:
            del timeout_sec
            return next(self._commands, None)

    console = rich_console.Console(
        width=120,
        height=20,
        force_terminal=True,
        record=True,
    )

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("crsbench.distributed.queue_monitor._RichMonitorInput", DummyInput),
        patch("rich.console.Console", return_value=console),
        patch("rich.live.Live", DummyLive),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(),
            use_rich=True,
            poll_interval=1.0,
        )

    assert len(rendered_updates) >= 2
    assert refresh_flags
    assert refresh_flags[0] is True
    running_table = rendered_updates[1].renderables[1]
    assert "Page 2/2: showing 2 of 6 running jobs;" in running_table.caption
    assert "n/p active; auto-rotates when idle" in running_table.caption
    assert list(running_table.columns[0].cells) == ["worker-4", "worker-5"]
    assert list(running_table.columns[6].cells) == ["4", "5"]
    assert list(running_table.columns[7].cells) == ["running", "running"]
    assert list(running_table.columns[8].cells) == ["1m0s", "1m0s"]


def test_display_worker_name_trims_cloud_experiment_prefix() -> None:
    assert (
        _display_worker_name(
            "afc-bugfinding-multilang",
            "crsbench-afc-bugfinding-multilang-work-003-abc123",
        )
        == "work-003-abc123"
    )


def test_display_worker_name_preserves_non_cloud_worker_names() -> None:
    assert _display_worker_name("exp-1", "worker-1") == "worker-1"


def test_monitor_queue_snapshot_callback_receives_current_snapshot() -> None:
    queue = MagicMock()
    active = QueueMonitorSnapshot(
        stats={"queued": 2, "started": 1, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 1, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    on_snapshot = MagicMock()

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(on_snapshot=on_snapshot),
            use_rich=False,
            poll_interval=0,
        )

    assert on_snapshot.call_args_list == [call(active), call(done)]


def test_monitor_queue_retries_finished_callback_until_processed() -> None:
    queue = MagicMock()
    callbacks = QueueMonitorCallbacks(
        on_job_finished=MagicMock(side_effect=[False, True])
    )

    active = QueueMonitorSnapshot(
        stats={"queued": 1, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    job = MagicMock()
    job.id = "job-1"
    job.is_failed = False

    refresh_calls = {"count": 0}

    def _refresh() -> None:
        refresh_calls["count"] += 1
        job.is_finished = True

    job.refresh.side_effect = _refresh

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, active],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_jobs=[job],
            callbacks=callbacks,
            use_rich=False,
            poll_interval=0,
        )

    assert callbacks.on_job_finished.call_count == 2
    assert refresh_calls["count"] == 2


def test_monitor_queue_clears_stale_failed_tracking_when_job_reactivates() -> None:
    queue = MagicMock()
    callbacks = QueueMonitorCallbacks(
        on_job_finished=MagicMock(return_value=True),
        on_job_failed=MagicMock(return_value=True),
    )

    active = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 1, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    retrying_job = MagicMock()
    retrying_job.id = "job-retry"

    retry_states = iter(
        [
            (False, True),
            (False, False),
            (False, False),
            (True, False),
        ]
    )

    def _refresh_retrying() -> None:
        is_finished, is_failed = next(retry_states)
        retrying_job.is_finished = is_finished
        retrying_job.is_failed = is_failed

    retrying_job.refresh.side_effect = _refresh_retrying

    peer_job = MagicMock()
    peer_job.id = "job-peer"

    peer_states = iter(
        [
            (False, False),
            (False, False),
            (True, False),
            (True, False),
        ]
    )

    def _refresh_peer() -> None:
        is_finished, is_failed = next(peer_states)
        peer_job.is_finished = is_finished
        peer_job.is_failed = is_failed

    peer_job.refresh.side_effect = _refresh_peer

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, active, active, active],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_jobs=[retrying_job, peer_job],
            callbacks=callbacks,
            use_rich=False,
            poll_interval=0,
        )

    callbacks.on_job_failed.assert_called_once_with(retrying_job)
    assert callbacks.on_job_finished.call_count == 2
    callbacks.on_job_finished.assert_any_call(peer_job)
    callbacks.on_job_finished.assert_any_call(retrying_job)


def test_monitor_queue_tolerates_transient_refresh_errors() -> None:
    queue = MagicMock()
    callbacks = QueueMonitorCallbacks(on_job_finished=MagicMock(return_value=True))

    active = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 1, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 1, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    job = MagicMock()
    job.id = "job-1"
    job.is_failed = False

    refresh_calls = {"count": 0}

    def _refresh() -> None:
        refresh_calls["count"] += 1
        if refresh_calls["count"] == 1:
            raise RuntimeError("redis down")
        job.is_finished = True

    job.refresh.side_effect = _refresh

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            side_effect=[active, done],
        ),
        patch("crsbench.distributed.queue_monitor.time.sleep"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_jobs=[job],
            callbacks=callbacks,
            use_rich=False,
            poll_interval=0,
        )

    assert refresh_calls["count"] == 2
    callbacks.on_job_finished.assert_called_once_with(job)


def test_monitor_queue_attach_mode_can_wait_while_idle() -> None:
    queue = MagicMock()
    idle = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 0, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            return_value=idle,
        ),
        patch(
            "crsbench.distributed.queue_monitor.time.sleep",
            side_effect=RuntimeError("stop monitoring"),
        ),
        pytest.raises(RuntimeError, match="stop monitoring"),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(),
            use_rich=False,
            poll_interval=0,
            exit_when_idle=False,
        )


def test_monitor_queue_plain_and_rich_paths_share_snapshot_builder() -> None:
    queue = MagicMock()
    done = QueueMonitorSnapshot(
        stats={"queued": 0, "started": 0, "finished": 1, "failed": 0, "workers": 1},
        running_jobs=[],
    )

    with patch(
        "crsbench.distributed.queue_monitor.build_monitor_snapshot",
        return_value=done,
    ) as plain_builder:
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(),
            use_rich=False,
            poll_interval=0,
        )

    assert plain_builder.call_count == 1

    class DummyLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *args, **kwargs):
            return None

    with (
        patch(
            "crsbench.distributed.queue_monitor.build_monitor_snapshot",
            return_value=done,
        ) as rich_builder,
        patch("rich.live.Live", DummyLive),
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(),
            use_rich=True,
            poll_interval=0,
        )

    assert rich_builder.call_count == 1


def test_monitor_queue_auto_detection_requires_interactive_stdout() -> None:
    queue = MagicMock()

    with (
        patch(
            "crsbench.distributed.queue_monitor.importlib.util.find_spec",
            return_value=object(),
        ),
        patch("sys.stdout.isatty", return_value=False),
        patch("crsbench.distributed.queue_monitor._monitor_queue_basic") as basic,
        patch("crsbench.distributed.queue_monitor._monitor_queue_rich") as rich,
    ):
        monitor_queue(
            queue,
            "exp-1",
            tracked_job_ids=None,
            callbacks=QueueMonitorCallbacks(),
        )

    basic.assert_called_once()
    rich.assert_not_called()
