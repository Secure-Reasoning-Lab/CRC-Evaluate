"""Tests for the shared distributed queue monitor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from crsbench.distributed.queue_monitor import (
    QueueMonitorCallbacks,
    QueueMonitorSnapshot,
    build_monitor_snapshot,
    monitor_queue,
)


def _existing_jobs(
    *,
    queued: int = 0,
    started: dict[str, object] | None = None,
    finished: int = 0,
    failed: int = 0,
) -> dict[str, dict[str, object]]:
    started_jobs = started or {}
    return {
        "queued": {f"q{i}": MagicMock() for i in range(queued)},
        "started": started_jobs,
        "finished": {f"f{i}": MagicMock() for i in range(finished)},
        "failed": {f"x{i}": MagicMock() for i in range(failed)},
        "deferred": {},
        "scheduled": {},
    }


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
            "crsbench.distributed.queue_monitor.get_existing_trials",
            return_value=_existing_jobs(
                queued=2,
                started={"started-1": started_job},
                finished=3,
                failed=1,
            ),
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
