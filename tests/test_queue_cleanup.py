"""Tests for experiment-scoped queue cleanup helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import crsbench.distributed.queue as queue_module
import pytest
from crsbench.distributed.queue import clear_experiment_jobs, get_trial_key
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
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [object()],
            "started": [],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
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
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [object()],
            "started": [object()],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
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


def test_clear_experiment_jobs_removes_all_physical_duplicate_jobs(monkeypatch) -> None:
    queue = MagicMock()
    job_a = MagicMock()
    job_a.id = "job-a"
    job_b = MagicMock()
    job_b.id = "job-b"
    removed_ids: list[str] = []

    monkeypatch.setattr(queue_module, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(
        queue_module,
        "get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [job_a, job_b],
            "started": [],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
        },
    )
    monkeypatch.setattr(
        queue_module,
        "remove_job_by_id",
        lambda _queue, job_id: removed_ids.append(job_id) or True,
    )

    removed = clear_experiment_jobs(queue, "exp-test")

    assert removed == 2
    assert removed_ids == ["job-a", "job-b"]


def test_get_existing_trial_jobs_filters_to_requested_experiment(monkeypatch) -> None:
    queue = MagicMock()
    queue.connection = MagicMock()
    queue.get_job_ids.return_value = ["queued-test", "queued-other"]
    queue.started_job_registry.get_job_ids.return_value = [
        "started-test",
        "started-other",
    ]
    queue.finished_job_registry.get_job_ids.return_value = []
    queue.failed_job_registry.get_job_ids.return_value = []
    queue.deferred_job_registry.get_job_ids.return_value = []

    queued_test = MagicMock()
    queued_test.id = "queued-test"
    queued_test.is_queued = True
    queued_test.meta = {"experiment_name": "exp-test"}

    queued_other = MagicMock()
    queued_other.id = "queued-other"
    queued_other.is_queued = True
    queued_other.meta = {"experiment_name": "exp-other"}

    started_test = MagicMock()
    started_test.id = "started-test"
    started_test.meta = {"experiment_name": "exp-test"}

    started_other = MagicMock()
    started_other.id = "started-other"
    started_other.meta = {"experiment_name": "exp-other"}

    jobs_by_id = {
        "queued-test": queued_test,
        "queued-other": queued_other,
        "started-test": started_test,
        "started-other": started_other,
    }

    monkeypatch.setattr(queue_module, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(
        queue_module, "get_all_jobs", lambda _queue: [queued_test, queued_other]
    )
    monkeypatch.setattr(
        queue_module.rq.job,  # type: ignore[union-attr]
        "Job",
        SimpleNamespace(fetch=lambda job_id, **_kwargs: jobs_by_id[job_id]),
    )

    existing = queue_module.get_existing_trial_jobs(queue, experiment_name="exp-test")

    assert existing["queued"] == [queued_test]
    assert existing["started"] == [started_test]
    assert existing["failed"] == []
    assert existing["finished"] == []


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


def test_handle_orphaned_jobs_accepts_physical_job_list(monkeypatch) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )

    handled = queue_module.handle_orphaned_jobs(queue, [job])

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
