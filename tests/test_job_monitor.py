"""Unit tests for JobMonitorLoop — stale detection, recovery, resume, lock takeover."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Fake Redis (extended from Plan 01 patterns)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal fake Redis for unit testing, with hash, list, and set/get support."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}
        self._strings: dict[str, str] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def hdel(self, key: str, field: str) -> int:
        h = self._hashes.get(key, {})
        if field in h:
            del h[field]
            return 1
        return 0

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)
        self._lists.pop(key, None)
        self._strings.pop(key, None)

    def rpush(self, key: str, value: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.append(value)
        return len(lst)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start : stop + 1]

    # String ops for lock support
    def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool:
        if nx and key in self._strings:
            return False
        self._strings[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self._strings.get(key)

    def ttl(self, key: str) -> int:
        """Return 300 for known keys, -2 for missing keys."""
        if key in self._strings:
            return 300
        return -2

    def eval(self, script: str, numkeys: int, *args: object) -> int:
        """Stub eval — always succeeds for unlock/renew scripts."""
        return 1

    def publish(self, channel: str, message: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stale_ts(seconds: int = 300) -> str:
    """Return ISO UTC timestamp that is `seconds` ago."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _fresh_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_store_with_job(
    fake: _FakeRedis,
    experiment: str,
    job_id: str,
    trial_key: str,
    state: str,
    retry_count: int = 0,
    claimed_by: str | None = "worker-1",
    heartbeat_age_seconds: int | None = None,
):
    """Insert a job record and optional heartbeat into the fake store."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    store = JobLifecycleStore(fake)
    record = JobLifecycleRecord(
        job_id=job_id,
        trial_key=trial_key,
        state=JobState(state),
        claimed_by=claimed_by,
        retry_count=retry_count,
    )
    store.set(experiment, record)
    if heartbeat_age_seconds is not None:
        fake.hset(
            f"crsbench:heartbeats:{experiment}",
            job_id,
            _stale_ts(heartbeat_age_seconds),
        )
    return store


# ---------------------------------------------------------------------------
# Task 1: JobMonitorLoop stale detection and recovery
# ---------------------------------------------------------------------------


def test_stale_heartbeat_detected() -> None:
    """Job with heartbeat >3min old + cloud API confirms dead -> transitions to ORPHANED."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp1", "j1", "trial-a", "running", heartbeat_age_seconds=400
    )

    # Dead instance
    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp1",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
        max_retries=3,
    )

    # Two scans needed (grace period)
    monitor._scan_and_recover()
    record = store.get("exp1", "j1")
    assert record is not None
    assert record.state is JobState.RUNNING  # grace period, not yet orphaned

    monitor._scan_and_recover()
    record = store.get("exp1", "j1")
    assert record is not None
    # After second scan with no artifacts and under max_retries -> QUEUED
    assert record.state in {JobState.QUEUED, JobState.ORPHANED, JobState.FAILED}


def test_alive_instance_not_orphaned() -> None:
    """Job with stale heartbeat but alive cloud instance is NOT orphaned."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp2", "j2", "trial-b", "running", heartbeat_age_seconds=400
    )

    liveness = MagicMock(return_value=True)  # alive!
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp2",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    monitor._scan_and_recover()
    monitor._scan_and_recover()

    record = store.get("exp2", "j2")
    assert record is not None
    assert record.state is JobState.RUNNING  # still running, not orphaned


def test_grace_period_before_requeue() -> None:
    """First scan flags stale; job is NOT acted on until second scan cycle."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp3", "j3", "trial-c", "running", heartbeat_age_seconds=300
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp3",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    # First scan: job should still be in RUNNING (grace period)
    monitor._scan_and_recover()
    record = store.get("exp3", "j3")
    assert record is not None
    assert record.state is JobState.RUNNING, "Grace period: first scan must NOT act"


def test_artifact_check_prevents_requeue() -> None:
    """Orphaned job with published valid artifacts -> COMPLETED, not requeued."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp4", "j4", "trial-d", "running", heartbeat_age_seconds=300
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=JobState.COMPLETED)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp4",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    # Two scans to pass grace period
    monitor._scan_and_recover()
    monitor._scan_and_recover()

    record = store.get("exp4", "j4")
    assert record is not None
    assert record.state is JobState.COMPLETED


def test_fail_artifact_marks_failed_without_requeue() -> None:
    """Published fail markers must prevent retries after a stale heartbeat."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp4b", "j4b", "trial-d", "running", heartbeat_age_seconds=300
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=JobState.FAILED)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp4b",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    monitor._scan_and_recover()
    monitor._scan_and_recover()

    record = store.get("exp4b", "j4b")
    assert record is not None
    assert record.state is JobState.FAILED

    events = fake.lrange("crsbench:recovery-events:exp4b", 0, -1)
    event_types = [json.loads(e)["event"] for e in events]
    assert "failed_from_artifact" in event_types


def test_max_retries_permanently_failed() -> None:
    """Orphaned job with retry_count >= max_retries -> FAILED with appropriate detail."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake,
        "exp5",
        "j5",
        "trial-e",
        "running",
        retry_count=3,
        heartbeat_age_seconds=300,
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp5",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
        max_retries=3,
    )

    monitor._scan_and_recover()
    monitor._scan_and_recover()

    record = store.get("exp5", "j5")
    assert record is not None
    assert record.state is JobState.FAILED
    assert record.detail is not None
    assert "permanently failed" in record.detail.lower()
    assert "max retries" in record.detail.lower()


def test_requeue_under_max_retries() -> None:
    """Orphaned job with retry_count < max_retries and no artifacts -> requeued (QUEUED)."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop
    from rq.job import JobStatus

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake,
        "exp6",
        "j6",
        "trial-f",
        "running",
        retry_count=0,
        heartbeat_age_seconds=300,
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp6",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
        max_retries=3,
    )

    fetched_job = MagicMock()
    fetched_job.origin = "crsbench_trial"
    queue = MagicMock()

    with (
        patch("rq.job.Job.fetch", return_value=fetched_job),
        patch("rq.Queue", return_value=queue),
    ):
        monitor._scan_and_recover()
        monitor._scan_and_recover()

    record = store.get("exp6", "j6")
    assert record is not None
    assert record.state is JobState.QUEUED
    assert record.retry_count == 1
    fetched_job.set_status.assert_called_once_with(JobStatus.FAILED)
    queue.enqueue_job.assert_called_once_with(fetched_job)


def test_retry_budget_allows_exact_final_retry() -> None:
    """A job at max_retries-1 gets one final requeue and lands exactly on the limit."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop
    from rq.job import JobStatus

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake,
        "exp6c",
        "j6c",
        "trial-f",
        "running",
        retry_count=2,
        heartbeat_age_seconds=300,
    )

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp6c",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=MagicMock(return_value=False),
        artifact_checker=MagicMock(return_value=False),
        max_retries=3,
    )

    fetched_job = MagicMock()
    fetched_job.origin = "crsbench_trial"
    queue = MagicMock()

    with (
        patch("rq.job.Job.fetch", return_value=fetched_job),
        patch("rq.Queue", return_value=queue),
    ):
        monitor._scan_and_recover()
        monitor._scan_and_recover()

    record = store.get("exp6c", "j6c")
    assert record is not None
    assert record.state is JobState.QUEUED
    assert record.retry_count == 3
    fetched_job.set_status.assert_called_once_with(JobStatus.FAILED)
    queue.enqueue_job.assert_called_once_with(fetched_job)


def test_requeue_failure_marks_failed() -> None:
    """If concrete RQ requeue fails, recovery must not claim the job is queued."""
    from crsbench.distributed.job_lifecycle import JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop
    from rq.job import JobStatus

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake,
        "exp6b",
        "j6b",
        "trial-f",
        "running",
        retry_count=0,
        heartbeat_age_seconds=300,
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp6b",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
        max_retries=3,
    )

    fetched_job = MagicMock()
    fetched_job.origin = "crsbench_trial"
    queue = MagicMock()
    queue.enqueue_job.side_effect = RuntimeError("enqueue boom")

    with (
        patch("rq.job.Job.fetch", return_value=fetched_job),
        patch("rq.Queue", return_value=queue),
    ):
        monitor._scan_and_recover()
        monitor._scan_and_recover()

    record = store.get("exp6b", "j6b")
    assert record is not None
    assert record.state is JobState.FAILED
    assert record.detail is not None
    assert "requeue failed" in record.detail.lower()
    fetched_job.set_status.assert_called_once_with(JobStatus.FAILED)
    queue.enqueue_job.assert_called_once_with(fetched_job)


def test_recovery_event_log() -> None:
    """Each recovery action appends an event to the Redis recovery list."""
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = _make_store_with_job(
        fake, "exp7", "j7", "trial-g", "running", heartbeat_age_seconds=300
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp7",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
        max_retries=3,
    )

    fetched_job = MagicMock()
    fetched_job.origin = "crsbench_trial"
    queue = MagicMock()

    with (
        patch("rq.job.Job.fetch", return_value=fetched_job),
        patch("rq.Queue", return_value=queue),
    ):
        monitor._scan_and_recover()
        monitor._scan_and_recover()

    events = fake.lrange("crsbench:recovery-events:exp7", 0, -1)
    assert len(events) >= 2

    event_types = [json.loads(e)["event"] for e in events]
    assert "orphan_detected" in event_types
    assert "requeued" in event_types


def test_resume_reconciles_uncollected() -> None:
    """reconcile_on_resume identifies jobs in SYNCING state as needing collection."""
    from crsbench.distributed.job_lifecycle import JobLifecycleStore, JobState
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)

    from crsbench.distributed.job_lifecycle import JobLifecycleRecord

    # One syncing job, one completed job
    store.set(
        "exp8",
        JobLifecycleRecord(
            job_id="j-syncing",
            trial_key="trial-sync",
            state=JobState.SYNCING,
            claimed_by="worker-2",
        ),
    )
    store.set(
        "exp8",
        JobLifecycleRecord(
            job_id="j-done",
            trial_key="trial-done",
            state=JobState.COMPLETED,
            claimed_by="worker-2",
        ),
    )

    liveness = MagicMock(return_value=False)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp8",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    needs_collection = monitor.reconcile_on_resume()
    assert "j-syncing" in needs_collection

    # Should have logged resume_reconcile events
    events = fake.lrange("crsbench:recovery-events:exp8", 0, -1)
    event_types = [json.loads(e)["event"] for e in events]
    assert "resume_reconcile" in event_types


def test_resume_reconcile_prefers_terminal_artifacts() -> None:
    """Published terminal artifacts should collapse syncing jobs to terminal on resume."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp8-artifact",
        JobLifecycleRecord(
            job_id="j-syncing",
            trial_key="trial-sync",
            state=JobState.SYNCING,
            claimed_by="worker-2",
        ),
    )

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp8-artifact",
        connection=fake,
        heartbeat_timeout_seconds=180,
        cloud_liveness_checker=MagicMock(return_value=False),
        artifact_checker=MagicMock(return_value=JobState.COMPLETED),
    )

    needs_collection = monitor.reconcile_on_resume()

    assert needs_collection == []
    record = store.get("exp8-artifact", "j-syncing")
    assert record is not None
    assert record.state is JobState.COMPLETED

    events = fake.lrange("crsbench:recovery-events:exp8-artifact", 0, -1)
    event_types = [json.loads(e)["event"] for e in events]
    assert "resume_completed_from_artifact" in event_types


def test_monitor_loop_start_stop() -> None:
    """Monitor thread starts and stops cleanly via Event."""
    from crsbench.distributed.job_lifecycle import JobLifecycleStore
    from crsbench.distributed.job_monitor import JobMonitorLoop

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)

    liveness = MagicMock(return_value=True)
    artifact_checker = MagicMock(return_value=False)

    monitor = JobMonitorLoop(
        lifecycle_store=store,
        experiment_name="exp9",
        connection=fake,
        heartbeat_timeout_seconds=180,
        scan_interval=100.0,  # long interval to avoid accidental scan
        cloud_liveness_checker=liveness,
        artifact_checker=artifact_checker,
    )

    monitor.start()
    assert monitor._thread is not None
    assert monitor._thread.is_alive()

    monitor.stop(timeout=2.0)
    # Thread should be stopped
    assert not monitor._thread.is_alive()


# ---------------------------------------------------------------------------
# Task 2: force_take_lock and try_resume_lock
# ---------------------------------------------------------------------------


def test_force_take_lock_expired() -> None:
    """When existing lock TTL < threshold, force_take_lock succeeds."""
    from crsbench.distributed.registry import RegistryClient

    class _ShortTTLRedis(_FakeRedis):
        """Redis where all keys report TTL below threshold."""

        def ttl(self, key: str) -> int:
            if key in self._strings:
                return 50  # below default threshold of 120
            return -2

        def eval(self, script: str, numkeys: int, *args: object) -> int:
            # Force-take Lua: always overwrite (TTL below threshold)
            key = args[0]
            new_token = args[1]
            self._strings[key] = new_token
            return 1

    fake = _ShortTTLRedis()
    # Simulate existing lock held by old owner
    fake._strings["crsbench:lock:exp-ft"] = "old-token"

    client = RegistryClient(fake)
    result = client.force_take_lock("exp-ft", ttl_threshold=120)
    assert result is True
    assert "exp-ft" in client._lock_tokens


def test_force_take_lock_active() -> None:
    """When existing lock TTL > threshold, force_take_lock returns False."""
    from crsbench.distributed.registry import RegistryClient

    class _LongTTLRedis(_FakeRedis):
        def ttl(self, key: str) -> int:
            if key in self._strings:
                return 500  # well above threshold
            return -2

        def eval(self, script: str, numkeys: int, *args: object) -> int:
            # Force-take Lua: refuse (TTL above threshold)
            return 0

    fake = _LongTTLRedis()
    fake._strings["crsbench:lock:exp-fa"] = "active-token"

    client = RegistryClient(fake)
    result = client.force_take_lock("exp-fa", ttl_threshold=120)
    assert result is False
    assert "exp-fa" not in client._lock_tokens


def test_try_resume_lock_succeeds() -> None:
    """Stale lock -> try_resume_lock takes over via force_take_lock."""
    from crsbench.distributed.registry import RegistryClient, RegistryLease

    class _StaleRedis(_FakeRedis):
        def ttl(self, key: str) -> int:
            # lock exists but TTL is low (stale)
            if key in self._strings:
                return 30
            return -2

        def eval(self, script: str, numkeys: int, *args: object) -> int:
            key = args[0]
            new_token = args[1]
            self._strings[key] = new_token
            return 1

    fake = _StaleRedis()
    fake._strings["crsbench:lock:exp-resume"] = "stale-owner-token"

    client = RegistryClient(fake)
    lease = RegistryLease(client, "exp-resume")

    assert not lease.lock_acquired
    result = lease.try_resume_lock()
    assert result is True
    assert lease.lock_acquired is True
