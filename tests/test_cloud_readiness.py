"""Tests for cloud worker readiness tracking and gating."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from crsbench.cloud.gce.models import GceWorkerRecord
from crsbench.cloud.records import CloudInstanceRecord
from crsbench.cloud.types import (
    CloudProvider,
    CloudProviderInstanceStatus,
    coerce_gce_provider_status,
)


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


def _make_cloud_instance(*, role: str = "worker") -> CloudInstanceRecord:
    return CloudInstanceRecord(
        provider=CloudProvider.GCE,
        role=role,
        name="gce-worker-001",
        instance_id="1001",
        status="RUNNING",
        zone="us-central1-a",
        internal_ip="10.0.0.10",
        labels={"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
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
        provider_status=CloudProviderInstanceStatus.PROVISIONING,
    )

    store.record(initial)
    store.record(replace(initial, state=CloudWorkerState.BOOTING))
    store.record(replace(initial, state=CloudWorkerState.REGISTERING))
    store.record(replace(initial, state=CloudWorkerState.READY, ready_at="now"))

    statuses = store.list_workers("exp-cloud-42")
    assert len(statuses) == 1
    assert statuses[0].instance_id == "1001"
    assert statuses[0].state is CloudWorkerState.READY
    assert statuses[0].provider_status is CloudProviderInstanceStatus.PROVISIONING


def test_readiness_store_keeps_evaluator_records_separate_from_workers() -> None:
    """Evaluator lifecycle records should not appear in worker readiness snapshots."""
    from crsbench.cloud.readiness import (
        CloudInstanceRole,
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )

    store = CloudReadinessStore(_FakeRedis())
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="worker-1001",
            instance_name="gce-worker-001",
            zone="us-central1-a",
            role=CloudInstanceRole.WORKER,
            state=CloudWorkerState.READY,
        )
    )
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="evaluator-2001",
            instance_name="gce-evaluator-001",
            zone="us-east1-b",
            role=CloudInstanceRole.EVALUATOR,
            state=CloudWorkerState.READY,
        )
    )

    worker_statuses = store.list_workers("exp-cloud-42")
    evaluator_statuses = store.list_workers(
        "exp-cloud-42", role=CloudInstanceRole.EVALUATOR
    )

    assert [status.instance_name for status in worker_statuses] == ["gce-worker-001"]
    assert [status.instance_name for status in evaluator_statuses] == [
        "gce-evaluator-001"
    ]


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
        provider_status=CloudProviderInstanceStatus.RUNNING,
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
            provider_status=CloudProviderInstanceStatus.RUNNING,
            detail="startup still in progress",
        )
    )

    timestamps = iter([0.0, 0.0, 0.0, 901.0])
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
            provider_status=CloudProviderInstanceStatus.RUNNING,
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


def test_wait_for_gce_workers_accepts_neutral_cloud_instance_records() -> None:
    """Shared readiness gating should not require provider-specific worker models."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )
    from crsbench.cloud.status import CloudFleetStatusManager

    store = CloudReadinessStore(_FakeRedis())
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="1001",
            instance_name="gce-worker-001",
            zone="us-central1-a",
            state=CloudWorkerState.READY,
            provider_status=CloudProviderInstanceStatus.RUNNING,
        )
    )

    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    snapshot = manager.wait_for_gce_workers(
        experiment_name="exp-cloud-42",
        workers=[_make_cloud_instance()],
        timeout_sec=900,
    )

    assert snapshot.ready_count == 1


def test_bring_up_workers_deletes_all_provider_neutral_placements_after_timeout() -> (
    None
):
    """Provider-neutral bring-up should tear down all created workers after a timeout."""
    from crsbench.cloud.readiness import CloudReadinessStore
    from crsbench.cloud.status import CloudFleetBringupError, CloudFleetStatusManager

    class _Adapter:
        def __init__(self) -> None:
            self.deleted: list[object] = []

        def create_workers(self, **_kwargs) -> list[GceWorkerRecord]:
            return [
                _make_worker(),
                replace(
                    _make_worker(),
                    name="gce-worker-002",
                    instance_id="1002",
                    zone="us-east1-b",
                    internal_ip="10.0.1.10",
                ),
            ]

        def delete_workers(self, *, plan) -> list[GceWorkerRecord]:
            self.deleted.append(plan)
            return []

        def max_worker_readiness_timeout(self, *, plan) -> int:
            del plan
            return 900

    store = CloudReadinessStore(_FakeRedis())
    adapter = _Adapter()
    plan = SimpleNamespace(
        experiment_name="exp-cloud-42",
        evaluator_placements=[SimpleNamespace()],
    )
    timestamps = iter([0.0, 0.0, 0.0, 901.0])
    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: next(timestamps),
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    with pytest.raises(CloudFleetBringupError, match="timed out waiting for ready"):
        manager.bring_up_workers(
            plan=plan,
            adapter=adapter,
            redis_host="redis.internal:6379",
            registration=object(),
        )

    assert adapter.deleted == [plan]


def test_coerce_gce_provider_status_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Unsupported GCE instance status"):
        coerce_gce_provider_status("MYSTERY_STATUS")


def test_bring_up_instances_deletes_worker_and_evaluator_placements_after_timeout() -> (
    None
):
    """Provider-neutral bring-up should tear down both roles after a timeout."""
    from crsbench.cloud.readiness import CloudReadinessStore
    from crsbench.cloud.status import CloudFleetBringupError, CloudFleetStatusManager

    class _Adapter:
        def __init__(self) -> None:
            self.deleted_workers: list[object] = []
            self.deleted_evaluators: list[object] = []

        def create_workers(self, **_kwargs) -> list[GceWorkerRecord]:
            return [_make_worker()]

        def create_evaluators(self, **_kwargs) -> list[GceWorkerRecord]:
            return [
                replace(
                    _make_worker(),
                    name="gce-evaluator-001",
                    instance_id="2001",
                    zone="us-east1-b",
                    internal_ip="10.0.2.10",
                    labels={
                        "crsbench-experiment": "exp-cloud-42",
                        "owner": "team-crs",
                        "crsbench-role": "evaluator",
                    },
                )
            ]

        def delete_workers(self, *, plan) -> list[GceWorkerRecord]:
            self.deleted_workers.append(plan)
            return []

        def delete_evaluators(self, *, plan) -> list[GceWorkerRecord]:
            self.deleted_evaluators.append(plan)
            return []

        def max_instance_readiness_timeout(self, *, plan) -> int:
            del plan
            return 900

    store = CloudReadinessStore(_FakeRedis())
    adapter = _Adapter()
    plan = SimpleNamespace(
        experiment_name="exp-cloud-42",
        evaluator_placements=[SimpleNamespace()],
    )
    timestamps = iter([0.0, 0.0, 0.0, 0.0, 901.0])
    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: next(timestamps),
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    with pytest.raises(CloudFleetBringupError, match="timed out waiting for ready"):
        manager.bring_up_instances(
            plan=plan,
            adapter=adapter,
            redis_host="redis.internal:6379",
            registration=object(),
            evaluator_experiment_config="experiment: exp-cloud-42\n",
        )

    assert adapter.deleted_workers == [plan]
    assert adapter.deleted_evaluators == [plan]


def test_wait_for_existing_workers_uses_adapter_expected_names_and_timeouts() -> None:
    """Shared readiness should not require GCE fleet configs for pre-provisioned workers."""
    from crsbench.cloud.readiness import (
        CloudReadinessStore,
        CloudWorkerState,
        CloudWorkerStatus,
    )
    from crsbench.cloud.status import CloudFleetStatusManager

    class _Adapter:
        def expected_worker_names(self, *, plan) -> list[str]:
            del plan
            return ["gce-worker-001"]

        def max_worker_readiness_timeout(self, *, plan) -> int:
            del plan
            return 900

        def list_workers(self, *, plan) -> list[GceWorkerRecord]:
            del plan
            return [_make_worker()]

    store = CloudReadinessStore(_FakeRedis())
    store.record(
        CloudWorkerStatus(
            experiment_name="exp-cloud-42",
            instance_id="1001",
            instance_name="gce-worker-001",
            zone="us-central1-a",
            state=CloudWorkerState.READY,
            provider_status=CloudProviderInstanceStatus.RUNNING,
        )
    )

    manager = CloudFleetStatusManager(
        readiness_store=store,
        provisioner=None,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        poll_interval_sec=0.0,
    )

    snapshot = manager.wait_for_existing_workers(
        plan=SimpleNamespace(experiment_name="exp-cloud-42"),
        adapter=_Adapter(),
    )

    assert snapshot.ready_count == 1
