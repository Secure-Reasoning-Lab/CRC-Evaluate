"""Tests for the shared distributed queue monitor."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from crsbench.distributed.queue_monitor import (
    QueueJobEntry,
    QueueMonitorCallbacks,
    QueueMonitorSnapshot,
    RunningJobInfo,
    _build_rich_group,
    _display_worker_name,
    _select_running_jobs_window,
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


def test_select_running_jobs_window_rotates_when_terminal_height_is_limited() -> None:
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

    (
        visible_jobs,
        next_cursor,
        rotation_active,
        page_index,
        page_count,
    ) = _select_running_jobs_window(
        console,
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        rotation_cursor=0,
    )

    assert rotation_active is True
    assert [job.trial_num for job in visible_jobs] == ["0", "1", "2", "3"]
    assert next_cursor == 1
    assert (page_index, page_count) == (0, 2)

    (
        rotated_jobs,
        wrapped_cursor,
        wrapped_rotation_active,
        wrapped_page_index,
        wrapped_page_count,
    ) = _select_running_jobs_window(
        console,
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        rotation_cursor=next_cursor,
    )

    assert wrapped_rotation_active is True
    assert [job.trial_num for job in rotated_jobs] == ["4", "5"]
    assert wrapped_cursor == 0
    assert (wrapped_page_index, wrapped_page_count) == (1, 2)


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

    (
        visible_jobs,
        next_cursor,
        rotation_active,
        page_index,
        page_count,
    ) = _select_running_jobs_window(
        console,
        snapshot,
        experiment_name="exp-1",
        total_jobs=6,
        disk_skipped=0,
        rotation_cursor=3,
    )

    assert rotation_active is False
    assert [job.trial_num for job in visible_jobs] == ["0", "1", "2", "3", "4", "5"]
    assert next_cursor == 0
    assert (page_index, page_count) == (0, 1)


def test_build_rich_group_caption_shows_page_indicator() -> None:
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
        rotation_active=True,
        rotation_page_index=1,
        rotation_page_count=2,
    )
    console = rich_console.Console(width=120, force_terminal=True, record=True)
    console.print(renderable)

    output = console.export_text()
    assert "Page 2/2: showing 4 of 6 running jobs; rotating each refresh" in output


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
