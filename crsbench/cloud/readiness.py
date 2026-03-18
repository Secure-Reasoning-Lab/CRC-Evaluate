"""Redis-backed readiness tracking for cloud-managed workers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from crsbench.cloud.types import CloudProviderInstanceStatus

if TYPE_CHECKING:
    from collections.abc import Mapping


class CloudWorkerState(StrEnum):
    """Operator-visible lifecycle states for cloud-backed workers."""

    PROVISIONING = "provisioning"
    BOOTING = "booting"
    REGISTERING = "registering"
    READY = "ready"
    BOOTSTRAP_FAILED = "bootstrap_failed"
    DELETING = "deleting"
    DELETED = "deleted"


class CloudInstanceRole(StrEnum):
    """Cloud runtime role tracked in the readiness store."""

    WORKER = "worker"
    EVALUATOR = "evaluator"


@dataclass(frozen=True)
class CloudWorkerStatus:
    """Serialized readiness/status record for one worker instance."""

    experiment_name: str
    instance_id: str
    instance_name: str
    zone: str
    state: CloudWorkerState
    role: CloudInstanceRole = CloudInstanceRole.WORKER
    provider_status: CloudProviderInstanceStatus | None = None
    internal_ip: str | None = None
    external_ip: str | None = None
    detail: str | None = None
    startup_evidence: str | None = None
    updated_at: str | None = None
    ready_at: str | None = None


class ReadinessRedisProtocol(Protocol):
    """Synchronous Redis operations used by the readiness store."""

    def hset(self, key: str, field: str, value: str) -> object: ...

    def hget(self, key: str, field: str) -> str | bytes | None: ...

    def hgetall(self, key: str) -> Mapping[str | bytes, str | bytes]: ...

    def delete(self, key: str) -> object: ...


@dataclass(frozen=True)
class CloudFleetSnapshot:
    """Current readiness view for a requested worker fleet."""

    experiment_name: str
    expected_instance_ids: tuple[str, ...]
    ready_workers: tuple[CloudWorkerStatus, ...]
    pending_workers: tuple[CloudWorkerStatus, ...]
    failed_workers: tuple[CloudWorkerStatus, ...]
    missing_instance_ids: tuple[str, ...]

    @property
    def requested_count(self) -> int:
        return len(self.expected_instance_ids)

    @property
    def ready_count(self) -> int:
        return len(self.ready_workers)


_ALLOWED_TRANSITIONS: dict[CloudWorkerState, set[CloudWorkerState]] = {
    CloudWorkerState.PROVISIONING: {
        CloudWorkerState.PROVISIONING,
        CloudWorkerState.BOOTING,
        CloudWorkerState.REGISTERING,
        CloudWorkerState.READY,
        CloudWorkerState.BOOTSTRAP_FAILED,
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.BOOTING: {
        CloudWorkerState.BOOTING,
        CloudWorkerState.REGISTERING,
        CloudWorkerState.READY,
        CloudWorkerState.BOOTSTRAP_FAILED,
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.REGISTERING: {
        CloudWorkerState.REGISTERING,
        CloudWorkerState.READY,
        CloudWorkerState.BOOTSTRAP_FAILED,
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.READY: {
        CloudWorkerState.READY,
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.BOOTSTRAP_FAILED: {
        CloudWorkerState.BOOTSTRAP_FAILED,
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.DELETING: {
        CloudWorkerState.DELETING,
        CloudWorkerState.DELETED,
    },
    CloudWorkerState.DELETED: {
        CloudWorkerState.DELETED,
    },
}

_FAILED_STATES = {
    CloudWorkerState.BOOTSTRAP_FAILED,
    CloudWorkerState.DELETED,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class CloudReadinessStore:
    """Persist cloud worker readiness records in Redis hashes per experiment."""

    def __init__(self, connection: ReadinessRedisProtocol) -> None:
        self._conn = connection

    def record(self, status: CloudWorkerStatus) -> CloudWorkerStatus:
        """Write a readiness record, enforcing forward-only state transitions."""
        existing = self.get_worker(
            status.experiment_name,
            status.instance_id,
            role=status.role,
        )
        if (
            existing is not None
            and status.state not in _ALLOWED_TRANSITIONS[existing.state]
        ):
            raise ValueError(
                f"Invalid readiness transition for {status.instance_name}: "
                f"{existing.state.value} -> {status.state.value}"
            )

        updated_at = status.updated_at or _utc_now()
        ready_at = status.ready_at
        if status.state is CloudWorkerState.READY and ready_at is None:
            ready_at = updated_at

        stored = CloudWorkerStatus(
            experiment_name=status.experiment_name,
            instance_id=status.instance_id,
            instance_name=status.instance_name,
            zone=status.zone,
            state=status.state,
            role=status.role,
            provider_status=status.provider_status,
            internal_ip=status.internal_ip,
            external_ip=status.external_ip,
            detail=status.detail,
            startup_evidence=status.startup_evidence,
            updated_at=updated_at,
            ready_at=ready_at,
        )
        payload = asdict(stored)
        payload["state"] = stored.state.value
        payload["role"] = stored.role.value
        if stored.provider_status is not None:
            payload["provider_status"] = stored.provider_status.value
        self._conn.hset(
            self._key(status.experiment_name, role=status.role),
            status.instance_id,
            json.dumps(payload, sort_keys=True),
        )
        return stored

    def get_worker(
        self,
        experiment_name: str,
        instance_id: str,
        *,
        role: CloudInstanceRole = CloudInstanceRole.WORKER,
    ) -> CloudWorkerStatus | None:
        """Fetch one worker record by experiment and instance id."""
        payload = self._conn.hget(self._key(experiment_name, role=role), instance_id)
        if payload is None:
            return None
        return self._deserialize(_decode_text(payload))

    def list_workers(
        self,
        experiment_name: str,
        *,
        role: CloudInstanceRole = CloudInstanceRole.WORKER,
    ) -> list[CloudWorkerStatus]:
        """List all known workers for an experiment."""
        payloads = self._conn.hgetall(self._key(experiment_name, role=role))
        workers: list[CloudWorkerStatus] = []
        for raw_payload in payloads.values():
            workers.append(self._deserialize(_decode_text(raw_payload)))
        workers.sort(key=lambda worker: worker.instance_name)
        return workers

    def snapshot(
        self,
        *,
        experiment_name: str,
        expected_instance_ids: list[str],
        role: CloudInstanceRole = CloudInstanceRole.WORKER,
    ) -> CloudFleetSnapshot:
        """Build a readiness view for the expected worker instance ids."""
        statuses = {
            worker.instance_id: worker
            for worker in self.list_workers(experiment_name, role=role)
        }
        ready_workers: list[CloudWorkerStatus] = []
        pending_workers: list[CloudWorkerStatus] = []
        failed_workers: list[CloudWorkerStatus] = []
        missing_instance_ids: list[str] = []

        for instance_id in expected_instance_ids:
            worker = statuses.get(instance_id)
            if worker is None:
                missing_instance_ids.append(instance_id)
                continue
            if worker.state is CloudWorkerState.READY:
                ready_workers.append(worker)
            elif worker.state in _FAILED_STATES:
                failed_workers.append(worker)
            else:
                pending_workers.append(worker)

        return CloudFleetSnapshot(
            experiment_name=experiment_name,
            expected_instance_ids=tuple(expected_instance_ids),
            ready_workers=tuple(ready_workers),
            pending_workers=tuple(pending_workers),
            failed_workers=tuple(failed_workers),
            missing_instance_ids=tuple(missing_instance_ids),
        )

    def clear_experiment(
        self,
        experiment_name: str,
        *,
        role: CloudInstanceRole = CloudInstanceRole.WORKER,
    ) -> None:
        """Remove all readiness records for one experiment."""
        self._conn.delete(self._key(experiment_name, role=role))

    def _key(self, experiment_name: str, *, role: CloudInstanceRole) -> str:
        return f"crsbench:cloud:{role.value}s:{experiment_name}"

    def _deserialize(self, payload: str) -> CloudWorkerStatus:
        data = json.loads(payload)
        data["role"] = CloudInstanceRole(data.get("role", CloudInstanceRole.WORKER))
        data["state"] = CloudWorkerState(data["state"])
        if data.get("provider_status") is not None:
            data["provider_status"] = CloudProviderInstanceStatus(
                data["provider_status"]
            )
        return CloudWorkerStatus(**data)
