"""Orchestrator-facing cloud worker status and readiness gating."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.cloud.readiness import (
    CloudFleetSnapshot,
    CloudReadinessStore,
    CloudWorkerState,
    CloudWorkerStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.distributed.registry import RuntimeRegistration
    from crsbench.validation.schemas import GceWorkerFleetConfig


class CloudFleetBringupError(RuntimeError):
    """Raised when cloud-backed workers never become explicitly ready."""

    def __init__(
        self,
        message: str,
        snapshot: CloudFleetSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class CloudFleetStatusManager:
    """Provision cloud workers and gate orchestration on explicit readiness."""

    def __init__(
        self,
        *,
        readiness_store: CloudReadinessStore,
        provisioner: GceProvisioner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_sec: float = 5.0,
    ) -> None:
        self._readiness_store = readiness_store
        self._provisioner = provisioner or GceProvisioner()
        self._clock = clock
        self._sleep = sleep
        self._poll_interval_sec = poll_interval_sec

    def bring_up_gce_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        redis_host: str,
        redis_password: str | None = None,
        registration: RuntimeRegistration,
    ) -> CloudFleetSnapshot:
        """Provision the requested GCE fleet and wait for explicit readiness."""
        self._readiness_store.clear_experiment(experiment_name)
        workers: list[GceWorkerRecord] = []
        try:
            workers = self._provisioner.create_workers(
                experiment_name=experiment_name,
                fleet=fleet,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
            )

            self._record_initial_workers(
                experiment_name=experiment_name,
                workers=workers,
            )
            return self.wait_for_gce_workers(
                experiment_name=experiment_name,
                workers=workers,
                timeout_sec=fleet.readiness_timeout_sec,
            )
        except Exception:
            if workers:
                self._teardown_gce_workers(
                    experiment_name=experiment_name,
                    fleet=fleet,
                    workers=workers,
                )
            raise

    def wait_for_existing_gce_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> CloudFleetSnapshot:
        """Wait for a pre-provisioned GCE fleet to appear and report readiness."""
        expected_names = self._provisioner.build_worker_names(
            experiment_name=experiment_name,
            fleet=fleet,
        )
        deadline = self._clock() + fleet.readiness_timeout_sec

        while True:
            workers = self._provisioner.list_workers(
                experiment_name=experiment_name,
                fleet=fleet,
            )
            workers_by_name = {worker.name: worker for worker in workers}
            missing_names = [
                worker_name
                for worker_name in expected_names
                if worker_name not in workers_by_name
            ]
            if not missing_names:
                expected_workers = [
                    workers_by_name[worker_name] for worker_name in expected_names
                ]
                self._record_initial_workers(
                    experiment_name=experiment_name,
                    workers=expected_workers,
                )
                remaining_timeout = max(int(deadline - self._clock()), 1)
                return self.wait_for_gce_workers(
                    experiment_name=experiment_name,
                    workers=expected_workers,
                    timeout_sec=remaining_timeout,
                )

            if self._clock() >= deadline:
                raise CloudFleetBringupError(
                    "timed out waiting for pre-provisioned GCE workers: "
                    + ", ".join(missing_names)
                )
            self._sleep(self._poll_interval_sec)

    def wait_for_gce_workers(
        self,
        *,
        experiment_name: str,
        workers: list[GceWorkerRecord],
        timeout_sec: int,
    ) -> CloudFleetSnapshot:
        """Wait until all expected workers report `ready` in the readiness store."""
        expected_instance_ids = [worker.instance_id for worker in workers]
        deadline = self._clock() + timeout_sec

        while True:
            snapshot = self._readiness_store.snapshot(
                experiment_name=experiment_name,
                expected_instance_ids=expected_instance_ids,
            )
            if snapshot.ready_count == snapshot.requested_count:
                return snapshot
            if snapshot.failed_workers:
                raise CloudFleetBringupError(
                    self._format_failure(
                        "cloud worker bootstrap failed",
                        snapshot,
                    ),
                    snapshot,
                )
            if self._clock() >= deadline:
                raise CloudFleetBringupError(
                    self._format_failure(
                        "timed out waiting for ready workers",
                        snapshot,
                    ),
                    snapshot,
                )
            self._sleep(self._poll_interval_sec)

    def _initial_state(self, worker: GceWorkerRecord) -> CloudWorkerState:
        provider_status = worker.status.upper()
        if provider_status == "RUNNING":
            return CloudWorkerState.BOOTING
        return CloudWorkerState.PROVISIONING

    def _record_initial_workers(
        self,
        *,
        experiment_name: str,
        workers: list[GceWorkerRecord],
    ) -> None:
        for worker in workers:
            if (
                self._readiness_store.get_worker(experiment_name, worker.instance_id)
                is not None
            ):
                continue
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=self._initial_state(worker),
                    provider_status=worker.status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail=f"GCE provider status: {worker.status}",
                )
            )

    def _format_failure(
        self,
        prefix: str,
        snapshot: CloudFleetSnapshot,
    ) -> str:
        parts = [f"{prefix}: ready {snapshot.ready_count}/{snapshot.requested_count}"]
        for worker in snapshot.failed_workers:
            evidence = worker.startup_evidence or worker.detail or "no startup evidence"
            parts.append(f"{worker.instance_name}={worker.state.value} ({evidence})")
        for worker in snapshot.pending_workers:
            evidence = worker.startup_evidence or worker.detail or "awaiting readiness"
            parts.append(f"{worker.instance_name}={worker.state.value} ({evidence})")
        for instance_id in snapshot.missing_instance_ids:
            parts.append(f"{instance_id}=missing (no readiness record)")
        return "; ".join(parts)

    def _teardown_gce_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        workers: list[GceWorkerRecord],
    ) -> None:
        for worker in workers:
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETING,
                    provider_status=worker.status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleting worker after failed bring-up",
                )
            )

        try:
            deleted_workers = self._provisioner.delete_workers(
                experiment_name=experiment_name,
                fleet=fleet,
            )
        except Exception:
            return
        for worker in deleted_workers:
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETED,
                    provider_status=worker.status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleted after failed bring-up",
                )
            )
