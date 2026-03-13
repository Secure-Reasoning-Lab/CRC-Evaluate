"""Tests for cloud worker readiness tracking and gating."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from crsbench.cloud.gce.models import GceWorkerRecord


class _FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)


def _make_worker() -> GceWorkerRecord:
    return GceWorkerRecord(
        name="gce-worker-001",
        instance_id="1001",
        status="RUNNING",
        zone="us-central1-a",
        internal_ip="10.0.0.10",
        external_ip=None,
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        labels={"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
        raw={},
    )


def test_readiness_store_tracks_identity_and_forward_transitions() -> None:
    """Store records by instance id and preserves forward lifecycle updates."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )

    store = CloudReadinessStore(_FakeRedis())
    initial = CloudWorkerStatus(
        experiment_name="exp-cloud-42",
        instance_id="1001",
        instance_name="gce-worker-001",
        zone="us-central1-a",
        state=CloudWorkerState.PROVISIONING,
        provider_status="PROVISIONING",
    )

    store.record(initial)
    store.record(replace(initial, state=CloudWorkerState.BOOTING))
    store.record(replace(initial, state=CloudWorkerState.REGISTERING))
    store.record(replace(initial, state=CloudWorkerState.READY, ready_at="now"))

    statuses = store.list_workers("exp-cloud-42")
    assert len(statuses) == 1
    assert statuses[0].instance_id == "1001"
    assert statuses[0].state is CloudWorkerState.READY


def test_readiness_store_rejects_backward_transition_after_ready() -> None:
    """Ready workers must not regress to booting states."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )

    store = CloudReadinessStore(_FakeRedis())
    ready_status = CloudWorkerStatus(
        experiment_name="exp-cloud-42",
        instance_id="1001",
        instance_name="gce-worker-001",
        zone="us-central1-a",
        state=CloudWorkerState.READY,
        provider_status="RUNNING",
    )

    store.record(ready_status)

    with pytest.raises(ValueError, match="Invalid readiness transition"):
        store.record(replace(ready_status, state=CloudWorkerState.BOOTING))


def test_wait_for_gce_workers_requires_explicit_ready_not_running_vm() -> None:
    """A VM reported as RUNNING is still non-ready until the readiness store says READY."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )
    from crsbench.cloud.status import CloudFleetBringupError, CloudFleetStatusManager

    store = CloudReadinessStore(_FakeRedis())
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="1001",
            instance_name="gce-worker-001",
            zone="us-central1-a",
            state=CloudWorkerState.BOOTING,
            provider_status="RUNNING",
            detail="startup still in progress",
        )
    )

    timestamps = iter([0.0, 901.0, 901.0])
    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: next(timestamps),
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    with pytest.raises(
        CloudFleetBringupError, match="timed out waiting for ready workers"
    ):
        manager.wait_for_gce_workers(
            experiment_name="exp-cloud-42",
            workers=[_make_worker()],
            timeout_sec=900,
        )


def test_wait_for_gce_workers_surfaces_startup_failure_evidence() -> None:
    """Bootstrap failures should surface stored evidence without needing SSH."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )
    from crsbench.cloud.status import CloudFleetBringupError, CloudFleetStatusManager

    store = CloudReadinessStore(_FakeRedis())
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="1001",
            instance_name="gce-worker-001",
            zone="us-central1-a",
            state=CloudWorkerState.BOOTSTRAP_FAILED,
            provider_status="RUNNING",
            detail="worker service failed",
            startup_evidence="systemd unit crsbench-worker.service exited with status 1",
        )
    )

    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    with pytest.raises(
        CloudFleetBringupError,
        match="systemd unit crsbench-worker.service exited with status 1",
    ):
        manager.wait_for_gce_workers(
            experiment_name="exp-cloud-42",
            workers=[_make_worker()],
            timeout_sec=900,
        )


def test_bring_up_gce_workers_deletes_fleet_after_timeout() -> None:
    """Bring-up timeouts should tear down the worker fleet instead of leaking VMs."""
    from crsbench.cloud.readiness import CloudReadinessStore
    from crsbench.cloud.status import CloudFleetBringupError, CloudFleetStatusManager

    class _Provisioner:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def create_workers(self, **_kwargs) -> list[GceWorkerRecord]:
            return [_make_worker()]

        def delete_workers(
            self, *, experiment_name: str, fleet
        ) -> list[GceWorkerRecord]:
            del fleet
            self.deleted.append(experiment_name)
            return [_make_worker()]

    store = CloudReadinessStore(_FakeRedis())
    provisioner = _Provisioner()
    timestamps = iter([0.0, 901.0, 901.0])
    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=provisioner,
        clock=lambda: next(timestamps),
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    with pytest.raises(CloudFleetBringupError, match="timed out waiting for ready"):
        manager.bring_up_gce_workers(
            experiment_name="exp-cloud-42",
            fleet=SimpleNamespace(readiness_timeout_sec=900),
            redis_host="redis.internal:6380",
            registration=object(),
        )

    assert provisioner.deleted == ["exp-cloud-42"]
