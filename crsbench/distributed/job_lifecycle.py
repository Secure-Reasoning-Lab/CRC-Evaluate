"""Redis-backed shadow state machine for CRSBench job lifecycle tracking."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Job state enumeration
# ---------------------------------------------------------------------------


class JobState(StrEnum):
    """Extended lifecycle states for cloud-backed CRSBench jobs."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SYNCING = "syncing"
    COMPLETED = "completed"
    FAILED = "failed"
    ORPHANED = "orphaned"


_JOB_ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.CLAIMED, JobState.FAILED},
    JobState.CLAIMED: {JobState.RUNNING, JobState.ORPHANED, JobState.FAILED},
    JobState.RUNNING: {JobState.SYNCING, JobState.ORPHANED, JobState.FAILED},
    JobState.SYNCING: {JobState.COMPLETED, JobState.ORPHANED, JobState.FAILED},
    # Terminal states
    JobState.COMPLETED: {JobState.COMPLETED},
    # Retry re-enters QUEUED
    JobState.FAILED: {JobState.QUEUED, JobState.FAILED},
    JobState.ORPHANED: {JobState.QUEUED, JobState.COMPLETED, JobState.FAILED},
}


# ---------------------------------------------------------------------------
# Data record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobLifecycleRecord:
    """Serialized lifecycle record for one CRSBench job."""

    job_id: str
    trial_key: str
    state: JobState
    claimed_by: str | None  # GCE instance name that claimed the job
    retry_count: int = 0
    last_heartbeat: str | None = None  # ISO UTC
    updated_at: str | None = None  # ISO UTC
    detail: str | None = None  # last event description


# ---------------------------------------------------------------------------
# Redis protocol
# ---------------------------------------------------------------------------


class LifecycleRedisProtocol(Protocol):
    """Synchronous Redis operations used by the lifecycle store."""

    def hset(self, key: str, field: str, value: str) -> object: ...

    def hget(self, key: str, field: str) -> str | bytes | None: ...

    def hgetall(self, key: str) -> Mapping[str | bytes, str | bytes]: ...

    def rpush(self, key: str, value: str) -> int: ...


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class JobLifecycleStore:
    """Persist and manage job lifecycle records in a Redis hash per experiment."""

    def __init__(self, connection: LifecycleRedisProtocol) -> None:
        self._conn = connection

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _key(self, experiment: str) -> str:
        return f"crsbench:jobs:{experiment}"

    def _heartbeat_key(self, experiment: str) -> str:
        return f"crsbench:heartbeats:{experiment}"

    # ------------------------------------------------------------------
    # Basic CRUD
    # ------------------------------------------------------------------

    def set(self, experiment: str, record: JobLifecycleRecord) -> None:
        """Serialize and write a lifecycle record to the experiment hash."""
        payload = asdict(record)
        payload["state"] = record.state.value
        self._conn.hset(
            self._key(experiment),
            record.job_id,
            json.dumps(payload, sort_keys=True),
        )

    def get(self, experiment: str, job_id: str) -> JobLifecycleRecord | None:
        """Fetch one lifecycle record by experiment and job id."""
        raw = self._conn.hget(self._key(experiment), job_id)
        if raw is None:
            return None
        return self._deserialize(_decode(raw))

    def list_jobs(self, experiment: str) -> list[JobLifecycleRecord]:
        """List all known lifecycle records for an experiment."""
        payloads = self._conn.hgetall(self._key(experiment))
        records: list[JobLifecycleRecord] = []
        for raw in payloads.values():
            records.append(self._deserialize(_decode(raw)))
        return records

    # ------------------------------------------------------------------
    # Transition
    # ------------------------------------------------------------------

    def transition(
        self,
        experiment: str,
        job_id: str,
        new_state: JobState,
        **kwargs: Any,
    ) -> JobLifecycleRecord:
        """Read-modify-write with forward-only transition validation.

        Extra keyword arguments are applied as field overrides on the new record.
        Supported overrides: claimed_by, detail.
        """
        record = self.get(experiment, job_id)
        if record is None:
            raise ValueError(f"Job {job_id!r} not found in experiment {experiment!r}")

        allowed = _JOB_ALLOWED_TRANSITIONS[record.state]
        if new_state not in allowed:
            raise ValueError(
                f"Invalid lifecycle transition for {job_id!r}: "
                f"{record.state.value} -> {new_state.value}"
            )

        # Build the updated record using dataclasses.replace
        update: dict[str, Any] = {
            "state": new_state,
            "updated_at": _utc_now(),
        }
        # Apply allowed overrides
        for field in ("claimed_by", "detail"):
            if field in kwargs:
                update[field] = kwargs[field]

        updated = replace(record, **update)
        self.set(experiment, updated)
        logger.debug(
            "Job {} transitioned {} -> {}",
            job_id,
            record.state.value,
            new_state.value,
        )
        return updated

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def update_heartbeat(self, experiment: str, job_id: str) -> None:
        """Write the current UTC timestamp as this job's heartbeat."""
        self._conn.hset(self._heartbeat_key(experiment), job_id, _utc_now())

    def get_heartbeat(self, experiment: str, job_id: str) -> str | None:
        """Return the last heartbeat ISO timestamp for a job, or None."""
        raw = self._conn.hget(self._heartbeat_key(experiment), job_id)
        if raw is None:
            return None
        return _decode(raw)

    # ------------------------------------------------------------------
    # Retry counter
    # ------------------------------------------------------------------

    def increment_retry(self, experiment: str, job_id: str) -> int:
        """Increment the retry counter for a job and return the new count."""
        record = self.get(experiment, job_id)
        if record is None:
            raise ValueError(f"Job {job_id!r} not found in experiment {experiment!r}")
        updated = replace(
            record, retry_count=record.retry_count + 1, updated_at=_utc_now()
        )
        self.set(experiment, updated)
        return updated.retry_count

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    def _deserialize(self, payload: str) -> JobLifecycleRecord:
        data = json.loads(payload)
        data["state"] = JobState(data["state"])
        return JobLifecycleRecord(**data)


# ---------------------------------------------------------------------------
# Recovery event log
# ---------------------------------------------------------------------------


def log_recovery_event(
    connection: LifecycleRedisProtocol,
    experiment: str,
    event_dict: dict[str, Any],
) -> None:
    """Append a JSON recovery event to the per-experiment audit log."""
    entry = {**event_dict, "ts": _utc_now()}
    connection.rpush(
        f"crsbench:recovery-events:{experiment}",
        json.dumps(entry, sort_keys=True),
    )
    logger.debug("Recovery event logged for {}: {}", experiment, event_dict)
