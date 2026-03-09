"""Tests for experiment-scoped queue cleanup helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import crsbench.distributed.queue as queue_module
import pytest
from crsbench.distributed.queue import get_trial_key
from crsbench.distributed.queue_cleanup import clean_experiment_queues


def test_clean_experiment_queues_dry_run(monkeypatch) -> None:
    queue_trial = MagicMock()
    queue_build = MagicMock()
    queue_verify = MagicMock()
    queue_map = {
        "q-trial": queue_trial,
        "q-build": queue_build,
        "q-verify": queue_verify,
    }
    redis_conn = MagicMock()

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda name, **_kwargs: queue_map[name],
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trials",
        lambda _queue, **_kwargs: {
            "queued": {"a": object()},
            "started": {},
            "failed": {},
            "finished": {},
        },
    )
    clear_mock = MagicMock(return_value=1)
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs", clear_mock
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        dry_run=True,
    )

    assert result.matched_jobs == 3
    assert result.removed_jobs == 0
    assert not result.removed_registry_entry
    assert not result.removed_lock
    clear_mock.assert_not_called()


def test_clean_experiment_queues_applies_registry_and_lock(monkeypatch) -> None:
    queue = MagicMock()
    redis_conn = MagicMock()
    registry = MagicMock()

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-shared", "q-shared", "q-shared"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trials",
        lambda _queue, **_kwargs: {
            "queued": {"a": object()},
            "started": {"b": object()},
            "failed": {},
            "finished": {},
        },
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 2,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.RegistryClient",
        lambda _conn: registry,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial", "build", "verify"),
        dry_run=False,
    )

    assert result.queue_names == ("q-shared",)
    assert result.matched_jobs == 2
    assert result.removed_jobs == 2
    assert result.removed_registry_entry
    assert result.removed_lock
    registry.deregister.assert_called_once_with("exp-test")
    redis_conn.delete.assert_called_once_with("crsbench:lock:exp-test")


def test_get_trial_key_falls_back_for_non_trial_jobs() -> None:
    job = MagicMock()
    job.id = "job-123"
    job.meta = {"experiment_name": "exp-x"}
    assert get_trial_key(job) == "job:exp-x:job-123"


def test_handle_orphaned_jobs_requeues_when_only_unrelated_workers_exist(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    started_jobs = {"trial-1": job}

    other_queue_worker = SimpleNamespace(
        queues=[SimpleNamespace(name="crsbench_build")],
    )
    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [other_queue_worker],
    )

    handled = queue_module.handle_orphaned_jobs(queue, started_jobs)

    assert handled == 1
    job.set_status.assert_called_once_with(
        queue_module.rq.job.JobStatus.FAILED  # type: ignore[union-attr]
    )
    queue.enqueue_job.assert_called_once_with(job)


def test_handle_orphaned_jobs_skips_when_queue_worker_exists(monkeypatch) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    started_jobs = {"trial-1": job}

    trial_queue_worker = SimpleNamespace(
        queues=[SimpleNamespace(name="crsbench_trial")],
    )
    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [trial_queue_worker],
    )

    handled = queue_module.handle_orphaned_jobs(queue, started_jobs)

    assert handled == 0
    job.set_status.assert_not_called()
    queue.enqueue_job.assert_not_called()
