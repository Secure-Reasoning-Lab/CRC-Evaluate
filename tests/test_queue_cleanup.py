"""Tests for experiment-scoped queue cleanup helpers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import crsbench.distributed.queue as queue_module
import pytest
from crsbench.distributed.job_lifecycle import JobLifecycleRecord, JobState
from crsbench.distributed.queue import clear_experiment_jobs, get_trial_key
from crsbench.distributed.queue_cleanup import clean_experiment_queues


def test_remove_job_by_id_clears_canceled_registry(monkeypatch) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = SimpleNamespace(
        started_job_registry=MagicMock(),
        finished_job_registry=MagicMock(),
        failed_job_registry=MagicMock(),
        deferred_job_registry=MagicMock(),
        scheduled_job_registry=MagicMock(),
        canceled_job_registry=MagicMock(),
        remove=MagicMock(),
        connection=MagicMock(),
    )
    fetched_job = MagicMock()
    fetch_job = MagicMock(return_value=fetched_job)
    monkeypatch.setattr(queue_module.rq.job.Job, "fetch", fetch_job)  # type: ignore[union-attr]

    removed = queue_module.remove_job_by_id(queue, "job-1")

    assert removed is True
    queue.started_job_registry.remove.assert_called_once_with("job-1", delete_job=False)
    queue.finished_job_registry.remove.assert_called_once_with(
        "job-1", delete_job=False
    )
    queue.failed_job_registry.remove.assert_called_once_with("job-1", delete_job=False)
    queue.deferred_job_registry.remove.assert_called_once_with(
        "job-1", delete_job=False
    )
    queue.scheduled_job_registry.remove.assert_called_once_with(
        "job-1", delete_job=False
    )
    queue.canceled_job_registry.remove.assert_called_once_with(
        "job-1", delete_job=False
    )
    queue.remove.assert_called_once_with("job-1")
    fetch_job.assert_called_once_with("job-1", connection=queue.connection)
    fetched_job.delete.assert_called_once_with()


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
    redis_conn.delete.assert_any_call(
        "crsbench:dispatcher:exp-test:build_requests",
        "crsbench:dispatcher:exp-test:build_results",
        "crsbench:dispatcher:exp-test:build_attempts",
        "crsbench:dispatcher:exp-test:lineages",
        "crsbench:dispatcher:exp-test:verify_requests",
        "crsbench:dispatcher:exp-test:verify_results",
        "crsbench:dispatcher:exp-test:verify_attempts",
        "crsbench:dispatcher:exp-test:evaluators",
        "crsbench:dispatcher:exp-test:lease",
    )
    redis_conn.delete.assert_any_call("crsbench:lock:exp-test")


def test_clean_experiment_queues_discovers_dispatcher_local_queues(
    monkeypatch,
) -> None:
    redis_conn = MagicMock()
    queue_map = {
        "q-build": MagicMock(),
        "q-verify": MagicMock(),
        "crsbench_exp-test_eval-1_build": MagicMock(),
        "crsbench_exp-test_eval-1_verify": MagicMock(),
    }

    class _FakeQueue:
        @staticmethod
        def all(*, connection):
            assert connection is redis_conn
            return [
                SimpleNamespace(name="crsbench_exp-test_eval-1_build"),
                SimpleNamespace(name="crsbench_exp-test_eval-1_verify"),
                SimpleNamespace(name="crsbench_exp-other_eval-1_build"),
            ]

        def __new__(cls, name, **_kwargs):
            queue = queue_map[name]
            queue.name = name
            return queue

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr("crsbench.distributed.queue_cleanup.rq.Queue", _FakeQueue)
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

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("build", "verify"),
        include_registry=False,
        include_lock=False,
        dry_run=True,
    )

    assert result.queue_names == (
        "q-build",
        "q-verify",
        "crsbench_exp-test_eval-1_build",
        "crsbench_exp-test_eval-1_verify",
    )
    assert result.matched_jobs == 4


def test_clean_experiment_queues_clears_dispatcher_state_for_evaluator_scopes(
    monkeypatch,
) -> None:
    redis_conn = MagicMock()
    queue_map = {
        "q-build": MagicMock(),
        "q-verify": MagicMock(),
    }

    class _FakeQueue:
        @staticmethod
        def all(*, connection):
            assert connection is redis_conn
            return []

        def __new__(cls, name, **_kwargs):
            queue = queue_map[name]
            queue.name = name
            return queue

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr("crsbench.distributed.queue_cleanup.rq.Queue", _FakeQueue)
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [],
            "started": [],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
        },
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 0,
    )

    clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("build", "verify"),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    redis_conn.delete.assert_called_once_with(
        "crsbench:dispatcher:exp-test:build_requests",
        "crsbench:dispatcher:exp-test:build_results",
        "crsbench:dispatcher:exp-test:build_attempts",
        "crsbench:dispatcher:exp-test:lineages",
        "crsbench:dispatcher:exp-test:verify_requests",
        "crsbench:dispatcher:exp-test:verify_results",
        "crsbench:dispatcher:exp-test:verify_attempts",
        "crsbench:dispatcher:exp-test:evaluators",
        "crsbench:dispatcher:exp-test:lease",
    )


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


def test_clean_experiment_queues_trial_scope_clears_lifecycle_when_empty(
    monkeypatch,
) -> None:
    queue_trial = MagicMock()
    redis_conn = MagicMock()
    states = iter(
        [
            {
                "queued": [object()],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
            {
                "queued": [],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
        ]
    )

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_trial,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1
    redis_conn.delete.assert_called_once_with(
        "crsbench:jobs:exp-test",
        "crsbench:heartbeats:exp-test",
    )


def test_clean_experiment_queues_partial_scope_preserves_lifecycle_state(
    monkeypatch,
) -> None:
    queue_build = MagicMock()
    redis_conn = MagicMock()

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_build,
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
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("build",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1
    redis_conn.delete.assert_not_called()


def test_clean_experiment_queues_started_trial_job_preserves_lifecycle_state(
    monkeypatch,
) -> None:
    queue_trial = MagicMock()
    redis_conn = MagicMock()
    live_started_job = MagicMock()
    live_started_job.get_status.return_value = "started"
    live_started_job.started_at = datetime.now(timezone.utc)
    live_started_job.timeout = 600
    states = iter(
        [
            {
                "queued": [],
                "started": [live_started_job],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
            {
                "queued": [],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
        ]
    )

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_trial,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1
    redis_conn.delete.assert_not_called()


def test_clean_experiment_queues_stale_started_trial_job_clears_lifecycle_state(
    monkeypatch,
) -> None:
    queue_trial = MagicMock()
    redis_conn = MagicMock()
    stale_started_job = MagicMock()
    stale_started_job.get_status.return_value = "started"
    stale_started_job.started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    stale_started_job.timeout = 60
    states = iter(
        [
            {
                "queued": [],
                "started": [stale_started_job],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
            {
                "queued": [],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
        ]
    )

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_trial,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1
    redis_conn.delete.assert_called_once_with(
        "crsbench:jobs:exp-test",
        "crsbench:heartbeats:exp-test",
    )


def test_clean_experiment_queues_non_started_registry_residue_clears_lifecycle_state(
    monkeypatch,
) -> None:
    queue_trial = MagicMock()
    redis_conn = MagicMock()
    finished_registry_job = MagicMock()
    finished_registry_job.get_status.return_value = "finished"
    states = iter(
        [
            {
                "queued": [],
                "started": [finished_registry_job],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
            {
                "queued": [],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
        ]
    )

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_trial,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1
    redis_conn.delete.assert_called_once_with(
        "crsbench:jobs:exp-test",
        "crsbench:heartbeats:exp-test",
    )


def test_clean_experiment_queues_lifecycle_clear_is_best_effort(monkeypatch) -> None:
    queue_trial = MagicMock()
    redis_conn = MagicMock()
    states = iter(
        [
            {
                "queued": [object()],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
            {
                "queued": [],
                "started": [],
                "failed": [],
                "finished": [],
                "deferred": [],
                "scheduled": [],
            },
        ]
    )
    redis_conn.delete.side_effect = OSError("redis down")

    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.resolve_queue_names",
        lambda _experiment: ("q-trial", "q-build", "q-verify"),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.rq.Queue",
        lambda _name, **_kwargs: queue_trial,
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.get_existing_trial_jobs",
        lambda _queue, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        "crsbench.distributed.queue_cleanup.clear_experiment_jobs",
        lambda _queue, _experiment_name: 1,
    )

    result = clean_experiment_queues(
        redis_conn,
        experiment_name="exp-test",
        scopes=("trial",),
        include_registry=False,
        include_lock=False,
        dry_run=False,
    )

    assert result.removed_jobs == 1


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
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300
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
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300

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


def test_handle_orphaned_jobs_repairs_lifecycle_after_requeue(monkeypatch) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"experiment_name": "exp-test"}
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-old",
        retry_count=2,
    )
    lifecycle_store.transition.side_effect = [
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.ORPHANED,
            claimed_by=None,
            retry_count=2,
        ),
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.QUEUED,
            claimed_by=None,
            retry_count=3,
        ),
    ]
    lifecycle_store.increment_retry.return_value = 3

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )
    handled = queue_module.handle_orphaned_jobs(
        queue,
        [job],
        lifecycle_store=lifecycle_store,
    )

    assert handled == 1
    queue.enqueue_job.assert_called_once_with(job)
    lifecycle_store.transition.assert_any_call(
        "exp-test",
        "job-1",
        JobState.ORPHANED,
        claimed_by=None,
        detail="recovered stale started job",
    )
    lifecycle_store.increment_retry.assert_called_once_with("exp-test", "job-1")
    lifecycle_store.transition.assert_any_call(
        "exp-test",
        "job-1",
        JobState.QUEUED,
        claimed_by=None,
        detail="recovered stale started job",
    )


def test_handle_orphaned_jobs_rolls_back_lifecycle_when_metadata_write_fails(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"experiment_name": "exp-test"}
    job.save_meta.side_effect = RuntimeError("meta write failed")
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-old",
        retry_count=2,
    )
    lifecycle_store.transition.side_effect = [
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.ORPHANED,
            claimed_by=None,
            retry_count=2,
        ),
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.FAILED,
            claimed_by=None,
            retry_count=3,
        ),
    ]
    lifecycle_store.increment_retry.return_value = 3

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )

    handled = queue_module.handle_orphaned_jobs(
        queue,
        [job],
        lifecycle_store=lifecycle_store,
    )

    assert handled == 1
    queue.enqueue_job.assert_not_called()
    job.set_status.assert_called_once_with(
        queue_module.rq.job.JobStatus.FAILED  # type: ignore[union-attr]
    )
    assert lifecycle_store.transition.call_args_list == [
        call(
            "exp-test",
            "job-1",
            JobState.ORPHANED,
            claimed_by=None,
            detail="recovered stale started job",
        ),
        call(
            "exp-test",
            "job-1",
            JobState.FAILED,
            claimed_by=None,
            detail="started-job retry enqueue failed: retry metadata update failed: meta write failed",
        ),
    ]


def test_handle_orphaned_jobs_drops_stale_started_duplicate_when_active_peer_exists(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    stale_started = MagicMock()
    stale_started.id = "job-started"
    stale_started.meta = {"experiment_name": "exp-test"}
    stale_started.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    stale_started.timeout = 60
    stale_started.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    queued_duplicate = MagicMock()
    queued_duplicate.id = "job-queued"
    queued_duplicate.meta = {"experiment_name": "exp-test"}

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        queue_module,
        "get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [queued_duplicate],
            "started": [stale_started],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
        },
    )
    monkeypatch.setattr(
        queue_module,
        "get_trial_key",
        lambda job: "trial-1" if job.id in {"job-started", "job-queued"} else job.id,
    )
    removed_ids: list[str] = []
    monkeypatch.setattr(
        queue_module,
        "remove_job_by_id",
        lambda _queue, job_id: removed_ids.append(job_id) or True,
    )

    handled = queue_module.handle_orphaned_jobs(queue, [stale_started])

    assert handled == 1
    assert removed_ids == ["job-started"]
    queue.enqueue_job.assert_not_called()
    stale_started.set_status.assert_not_called()


def test_handle_orphaned_jobs_keeps_one_stale_started_survivor_per_trial(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    stale_a = MagicMock()
    stale_a.id = "job-a"
    stale_a.meta = {"experiment_name": "exp-test"}
    stale_a.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    stale_a.timeout = 60
    stale_a.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    stale_b = MagicMock()
    stale_b.id = "job-b"
    stale_b.meta = {"experiment_name": "exp-test"}
    stale_b.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    stale_b.timeout = 60
    stale_b.started_at = datetime.now(timezone.utc) - timedelta(minutes=11)

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        queue_module,
        "get_existing_trial_jobs",
        lambda _queue, **_kwargs: {
            "queued": [],
            "started": [stale_a, stale_b],
            "failed": [],
            "finished": [],
            "deferred": [],
            "scheduled": [],
        },
    )
    monkeypatch.setattr(queue_module, "get_trial_key", lambda _job: "trial-1")
    removed_ids: list[str] = []
    monkeypatch.setattr(
        queue_module,
        "remove_job_by_id",
        lambda _queue, job_id: removed_ids.append(job_id) or True,
    )

    handled = queue_module.handle_orphaned_jobs(queue, [stale_a, stale_b])

    assert handled == 2
    assert removed_ids == ["job-b"]
    queue.enqueue_job.assert_called_once_with(stale_a)
    stale_a.set_status.assert_called_once_with(
        queue_module.rq.job.JobStatus.FAILED  # type: ignore[union-attr]
    )
    stale_b.set_status.assert_not_called()


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


def test_handle_orphaned_jobs_keeps_fresh_started_job_without_workers(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc)
    job.timeout = 300
    started_jobs = {"trial-1": job}

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )

    handled = queue_module.handle_orphaned_jobs(queue, started_jobs)

    assert handled == 0
    job.set_status.assert_not_called()
    queue.enqueue_job.assert_not_called()


def test_handle_orphaned_jobs_recovers_stale_started_job_with_live_queue_worker(
    monkeypatch,
) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300
    started_jobs = {"trial-1": job}

    unrelated_live_worker = SimpleNamespace(
        name="worker-live",
        queues=[SimpleNamespace(name="crsbench_trial")],
    )
    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [unrelated_live_worker],
    )

    handled = queue_module.handle_orphaned_jobs(queue, started_jobs)

    assert handled == 1
    job.set_status.assert_called_once_with(
        queue_module.rq.job.JobStatus.FAILED  # type: ignore[union-attr]
    )
    queue.enqueue_job.assert_called_once_with(job)


def test_handle_orphaned_jobs_removes_terminal_started_job_residue(monkeypatch) -> None:
    if not queue_module.REDIS_AVAILABLE:
        pytest.skip("Redis/RQ not available")

    queue = MagicMock()
    queue.name = "crsbench_trial"
    queue.connection = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"experiment_name": "exp-test"}
    job.get_status.return_value = queue_module.rq.job.JobStatus.STARTED  # type: ignore[union-attr]
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
    job.timeout = 300

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.COMPLETED,
        claimed_by=None,
        retry_count=0,
    )

    monkeypatch.setattr(
        queue_module.rq.Worker,  # type: ignore[union-attr]
        "all",
        lambda **_kwargs: [],
    )
    remove_job = MagicMock(return_value=True)
    monkeypatch.setattr(queue_module, "remove_job_by_id", remove_job)

    handled = queue_module.handle_orphaned_jobs(
        queue,
        [job],
        lifecycle_store=lifecycle_store,
    )

    assert handled == 1
    remove_job.assert_called_once_with(queue, "job-1")
    job.set_status.assert_not_called()
    queue.enqueue_job.assert_not_called()
