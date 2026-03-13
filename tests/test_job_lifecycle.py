"""Unit tests for JobLifecycleStore state machine and Redis-backed store."""

from __future__ import annotations

import json


class _FakeRedis:
    """Minimal fake Redis for unit testing, with hash and list support."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)
        self._lists.pop(key, None)

    def rpush(self, key: str, value: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.append(value)
        return len(lst)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start : stop + 1]


# ---------------------------------------------------------------------------
# JobState enum
# ---------------------------------------------------------------------------


def test_job_state_enum() -> None:
    """JobState has exactly 7 members with correct string values."""
    from crsbench.distributed.job_lifecycle import JobState

    members = list(JobState)
    assert len(members) == 7
    values = {s.value for s in members}
    assert values == {
        "queued",
        "claimed",
        "running",
        "syncing",
        "completed",
        "failed",
        "orphaned",
    }


# ---------------------------------------------------------------------------
# Transition enforcement
# ---------------------------------------------------------------------------


def test_invalid_transition_raises() -> None:
    """Direct QUEUED->COMPLETED and COMPLETED->RUNNING are rejected."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    record = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-abc",
        state=JobState.QUEUED,
        claimed_by=None,
    )
    store.set("exp-1", record)

    import pytest

    with pytest.raises(ValueError, match="Invalid.*transition"):
        store.transition("exp-1", "job-1", JobState.COMPLETED)

    # Advance to COMPLETED via valid path then reject backward move
    store.transition("exp-1", "job-1", JobState.CLAIMED)
    store.transition("exp-1", "job-1", JobState.RUNNING)
    store.transition("exp-1", "job-1", JobState.SYNCING)
    store.transition("exp-1", "job-1", JobState.COMPLETED)

    with pytest.raises(ValueError, match="Invalid.*transition"):
        store.transition("exp-1", "job-1", JobState.RUNNING)


def test_valid_transitions() -> None:
    """QUEUED->CLAIMED->RUNNING->SYNCING->COMPLETED all succeed."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    record = JobLifecycleRecord(
        job_id="job-2",
        trial_key="trial-xyz",
        state=JobState.QUEUED,
        claimed_by=None,
    )
    store.set("exp-2", record)

    r1 = store.transition("exp-2", "job-2", JobState.CLAIMED)
    assert r1.state is JobState.CLAIMED

    r2 = store.transition("exp-2", "job-2", JobState.RUNNING)
    assert r2.state is JobState.RUNNING

    r3 = store.transition("exp-2", "job-2", JobState.SYNCING)
    assert r3.state is JobState.SYNCING

    r4 = store.transition("exp-2", "job-2", JobState.COMPLETED)
    assert r4.state is JobState.COMPLETED


def test_orphaned_transitions() -> None:
    """CLAIMED/RUNNING/SYNCING->ORPHANED succeed; ORPHANED->QUEUED/COMPLETED/FAILED succeed."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)

    def _make(jid: str, state: JobState) -> None:
        store.set(
            "exp-3",
            JobLifecycleRecord(
                job_id=jid, trial_key="trial-t", state=state, claimed_by=None
            ),
        )

    # CLAIMED -> ORPHANED
    _make("j-claimed", JobState.CLAIMED)
    r = store.transition("exp-3", "j-claimed", JobState.ORPHANED)
    assert r.state is JobState.ORPHANED

    # RUNNING -> ORPHANED
    _make("j-running", JobState.RUNNING)
    r = store.transition("exp-3", "j-running", JobState.ORPHANED)
    assert r.state is JobState.ORPHANED

    # SYNCING -> ORPHANED
    _make("j-syncing", JobState.SYNCING)
    r = store.transition("exp-3", "j-syncing", JobState.ORPHANED)
    assert r.state is JobState.ORPHANED

    # ORPHANED -> QUEUED (retry)
    _make("j-orphaned-retry", JobState.ORPHANED)
    r = store.transition("exp-3", "j-orphaned-retry", JobState.QUEUED)
    assert r.state is JobState.QUEUED

    # ORPHANED -> COMPLETED (artifact found)
    _make("j-orphaned-done", JobState.ORPHANED)
    r = store.transition("exp-3", "j-orphaned-done", JobState.COMPLETED)
    assert r.state is JobState.COMPLETED

    # ORPHANED -> FAILED (max retries)
    _make("j-orphaned-fail", JobState.ORPHANED)
    r = store.transition("exp-3", "j-orphaned-fail", JobState.FAILED)
    assert r.state is JobState.FAILED


def test_failed_to_queued_retry() -> None:
    """FAILED->QUEUED transition succeeds (retry re-queue)."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-4",
        JobLifecycleRecord(
            job_id="job-f", trial_key="trial-f", state=JobState.FAILED, claimed_by=None
        ),
    )
    r = store.transition("exp-4", "job-f", JobState.QUEUED)
    assert r.state is JobState.QUEUED


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------


def test_store_round_trip() -> None:
    """set() then get() returns an equivalent record."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    record = JobLifecycleRecord(
        job_id="job-rt",
        trial_key="trial-rt",
        state=JobState.QUEUED,
        claimed_by=None,
        retry_count=0,
        last_heartbeat=None,
        updated_at=None,
        detail=None,
    )
    store.set("exp-rt", record)

    fetched = store.get("exp-rt", "job-rt")
    assert fetched is not None
    assert fetched.job_id == "job-rt"
    assert fetched.trial_key == "trial-rt"
    assert fetched.state is JobState.QUEUED
    assert fetched.claimed_by is None
    assert fetched.retry_count == 0


def test_claim_records_instance() -> None:
    """Transitioning to CLAIMED with claimed_by persists the instance name."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    record = JobLifecycleRecord(
        job_id="job-claim",
        trial_key="trial-claim",
        state=JobState.QUEUED,
        claimed_by=None,
    )
    store.set("exp-claim", record)

    r = store.transition(
        "exp-claim", "job-claim", JobState.CLAIMED, claimed_by="worker-1"
    )
    assert r.claimed_by == "worker-1"

    fetched = store.get("exp-claim", "job-claim")
    assert fetched is not None
    assert fetched.claimed_by == "worker-1"


def test_sync_gates_completion() -> None:
    """RUNNING cannot transition directly to COMPLETED (must go through SYNCING)."""
    import pytest
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-sg",
        JobLifecycleRecord(
            job_id="job-sg",
            trial_key="trial-sg",
            state=JobState.RUNNING,
            claimed_by=None,
        ),
    )

    with pytest.raises(ValueError, match="Invalid.*transition"):
        store.transition("exp-sg", "job-sg", JobState.COMPLETED)


def test_list_jobs() -> None:
    """list_jobs returns all stored jobs for an experiment."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    for i in range(3):
        store.set(
            "exp-list",
            JobLifecycleRecord(
                job_id=f"job-{i}",
                trial_key=f"trial-{i}",
                state=JobState.QUEUED,
                claimed_by=None,
            ),
        )

    jobs = store.list_jobs("exp-list")
    assert len(jobs) == 3
    ids = {j.job_id for j in jobs}
    assert ids == {"job-0", "job-1", "job-2"}


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_update() -> None:
    """update_heartbeat writes a timestamp to the heartbeat hash."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-hb",
        JobLifecycleRecord(
            job_id="job-hb",
            trial_key="trial-hb",
            state=JobState.RUNNING,
            claimed_by=None,
        ),
    )

    store.update_heartbeat("exp-hb", "job-hb")
    ts = store.get_heartbeat("exp-hb", "job-hb")
    assert ts is not None
    # Should be a valid ISO timestamp
    from datetime import datetime

    datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Retry count
# ---------------------------------------------------------------------------


def test_retry_count_increment() -> None:
    """increment_retry increases retry_count by 1 and returns new count."""
    from crsbench.distributed.job_lifecycle import (
        JobLifecycleRecord,
        JobLifecycleStore,
        JobState,
    )

    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-rc",
        JobLifecycleRecord(
            job_id="job-rc",
            trial_key="trial-rc",
            state=JobState.FAILED,
            claimed_by=None,
            retry_count=0,
        ),
    )

    count = store.increment_retry("exp-rc", "job-rc")
    assert count == 1

    count = store.increment_retry("exp-rc", "job-rc")
    assert count == 2

    fetched = store.get("exp-rc", "job-rc")
    assert fetched is not None
    assert fetched.retry_count == 2


# ---------------------------------------------------------------------------
# Recovery event log
# ---------------------------------------------------------------------------


def test_recovery_event_log() -> None:
    """log_recovery_event appends a JSON event to a Redis list keyed per experiment."""
    from crsbench.distributed.job_lifecycle import log_recovery_event

    fake = _FakeRedis()
    log_recovery_event(fake, "exp-log", {"event": "orphan_detected", "job_id": "job-x"})
    log_recovery_event(fake, "exp-log", {"event": "requeued", "job_id": "job-x"})

    entries = fake.lrange("crsbench:recovery-events:exp-log", 0, -1)
    assert len(entries) == 2

    first = json.loads(entries[0])
    assert first["event"] == "orphan_detected"
    assert first["job_id"] == "job-x"
    assert "ts" in first

    second = json.loads(entries[1])
    assert second["event"] == "requeued"
