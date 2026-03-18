"""Orchestrator-facing cloud worker status and readiness gating."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.cloud.readiness import (
    CloudFleetSnapshot,
    CloudInstanceRole,
    CloudReadinessStore,
    CloudWorkerState,
    CloudWorkerStatus,
)
from crsbench.cloud.types import CloudProviderInstanceStatus, coerce_gce_provider_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import CloudLaunchPlan
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
        bootstrap_inputs: "CloudVmBootstrapInputs | None" = None,
        env_passthrough: dict[str, str] | None = None,
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
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
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

    def bring_up_instances(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
        redis_host: str,
        redis_password: str | None = None,
        registration: "RuntimeRegistration",
        bootstrap_inputs: "CloudVmBootstrapInputs | None" = None,
        worker_env_passthrough: dict[str, str] | None = None,
        evaluator_env_passthrough: dict[str, str] | None = None,
        evaluator_experiment_config: str | None = None,
    ) -> CloudFleetSnapshot:
        """Provision workers and evaluators across a launch plan and wait for readiness."""
        self._clear_experiment_roles(
            experiment_name=plan.experiment_name,
            roles=(
                CloudInstanceRole.WORKER,
                CloudInstanceRole.EVALUATOR,
            ),
        )
        workers: list[GceWorkerRecord] = []
        evaluators: list[GceWorkerRecord] = []
        try:
            workers = adapter.create_workers(
                plan=plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=worker_env_passthrough,
            )
            if plan.evaluator_placements:
                if evaluator_experiment_config is None:
                    raise ValueError(
                        "Evaluator placements require serialized experiment config"
                    )
                evaluators = adapter.create_evaluators(
                    plan=plan,
                    redis_host=redis_host,
                    redis_password=redis_password,
                    registration=registration,
                    experiment_config_path=evaluator_experiment_config,
                    bootstrap_inputs=bootstrap_inputs,
                    env_passthrough=evaluator_env_passthrough,
                )

            self._record_initial_workers(
                experiment_name=plan.experiment_name,
                workers=workers,
            )
            self._record_initial_workers(
                experiment_name=plan.experiment_name,
                workers=evaluators,
                role=CloudInstanceRole.EVALUATOR,
            )
            return self.wait_for_instances(
                experiment_name=plan.experiment_name,
                workers=workers,
                evaluators=evaluators,
                timeout_sec=self._max_instance_readiness_timeout(adapter, plan),
            )
        except Exception:
            if workers or evaluators:
                self._teardown_instances(
                    plan=plan,
                    adapter=adapter,
                    workers=workers,
                    evaluators=evaluators,
                )
            raise

    def wait_for_existing_instances(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
    ) -> CloudFleetSnapshot:
        """Wait for all pre-provisioned workers and evaluators to report ready."""
        expected_workers: list[str] = []
        expected_evaluators: list[str] = []
        timeout_sec = self._max_instance_readiness_timeout(adapter, plan)
        for fleet in adapter.build_worker_fleets(plan):
            expected_workers.extend(
                self._provisioner.build_worker_names(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )
        for fleet in adapter.build_evaluator_fleets(plan):
            expected_evaluators.extend(
                self._provisioner.build_worker_names(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )

        deadline = self._clock() + timeout_sec
        while True:
            workers = adapter.list_workers(plan=plan)
            evaluators = adapter.list_evaluators(plan=plan)
            workers_by_name = {worker.name: worker for worker in workers}
            evaluators_by_name = {worker.name: worker for worker in evaluators}
            missing_names = [
                worker_name
                for worker_name in expected_workers
                if worker_name not in workers_by_name
            ]
            missing_names.extend(
                evaluator_name
                for evaluator_name in expected_evaluators
                if evaluator_name not in evaluators_by_name
            )
            if not missing_names:
                resolved_workers = [
                    workers_by_name[worker_name] for worker_name in expected_workers
                ]
                resolved_evaluators = [
                    evaluators_by_name[evaluator_name]
                    for evaluator_name in expected_evaluators
                ]
                self._record_initial_workers(
                    experiment_name=plan.experiment_name,
                    workers=resolved_workers,
                )
                self._record_initial_workers(
                    experiment_name=plan.experiment_name,
                    workers=resolved_evaluators,
                    role=CloudInstanceRole.EVALUATOR,
                )
                remaining_timeout = max(int(deadline - self._clock()), 1)
                return self.wait_for_instances(
                    experiment_name=plan.experiment_name,
                    workers=resolved_workers,
                    evaluators=resolved_evaluators,
                    timeout_sec=remaining_timeout,
                )

            if self._clock() >= deadline:
                raise CloudFleetBringupError(
                    "timed out waiting for pre-provisioned cloud instances: "
                    + ", ".join(missing_names)
                )
            self._sleep(self._poll_interval_sec)

    def _initial_state(self, worker: GceWorkerRecord) -> CloudWorkerState:
        provider_status = coerce_gce_provider_status(worker.status)
        if provider_status is CloudProviderInstanceStatus.RUNNING:
            return CloudWorkerState.BOOTING
        return CloudWorkerState.PROVISIONING

    def _record_initial_workers(
        self,
        *,
        experiment_name: str,
        workers: list[GceWorkerRecord],
        role: CloudInstanceRole = CloudInstanceRole.WORKER,
    ) -> None:
        for worker in workers:
            provider_status = coerce_gce_provider_status(worker.status)
            if (
                self._readiness_store.get_worker(
                    experiment_name,
                    worker.instance_id,
                    role=role,
                )
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
                    role=role,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail=f"GCE provider status: {provider_status.value}",
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
            parts.append(
                f"{self._display_name(worker)}={worker.state.value} ({evidence})"
            )
        for worker in snapshot.pending_workers:
            evidence = worker.startup_evidence or worker.detail or "awaiting readiness"
            parts.append(
                f"{self._display_name(worker)}={worker.state.value} ({evidence})"
            )
        for instance_id in snapshot.missing_instance_ids:
            parts.append(f"{instance_id}=missing (no readiness record)")
        return "; ".join(parts)

    def _display_name(self, worker: CloudWorkerStatus) -> str:
        if worker.role is CloudInstanceRole.WORKER:
            return worker.instance_name
        return f"{worker.instance_name}[{worker.role.value}]"

    def _teardown_gce_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        workers: list[GceWorkerRecord],
    ) -> None:
        for worker in workers:
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETING,
                    provider_status=provider_status,
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
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETED,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleted after failed bring-up",
                )
            )

    def bring_up_workers(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
        redis_host: str,
        redis_password: str | None = None,
        registration: "RuntimeRegistration",
        bootstrap_inputs: "CloudVmBootstrapInputs | None" = None,
        env_passthrough: dict[str, str] | None = None,
    ) -> CloudFleetSnapshot:
        """Provision workers across all placements in a launch plan and wait for readiness."""
        self._readiness_store.clear_experiment(plan.experiment_name)
        workers: list[GceWorkerRecord] = []
        try:
            workers = adapter.create_workers(
                plan=plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
            )
            self._record_initial_workers(
                experiment_name=plan.experiment_name,
                workers=workers,
            )
            return self.wait_for_gce_workers(
                experiment_name=plan.experiment_name,
                workers=workers,
                timeout_sec=self._max_readiness_timeout(adapter, plan),
            )
        except Exception:
            if workers:
                self._teardown_workers(plan=plan, adapter=adapter, workers=workers)
            raise

    def wait_for_existing_workers(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
    ) -> CloudFleetSnapshot:
        """Wait for all pre-provisioned workers in a launch plan to appear and report ready."""
        expected_names: list[str] = []
        timeout_sec = self._max_readiness_timeout(adapter, plan)
        for fleet in adapter.build_worker_fleets(plan):
            expected_names.extend(
                self._provisioner.build_worker_names(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )

        deadline = self._clock() + timeout_sec
        while True:
            workers = adapter.list_workers(plan=plan)
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
                    experiment_name=plan.experiment_name,
                    workers=expected_workers,
                )
                remaining_timeout = max(int(deadline - self._clock()), 1)
                return self.wait_for_gce_workers(
                    experiment_name=plan.experiment_name,
                    workers=expected_workers,
                    timeout_sec=remaining_timeout,
                )

            if self._clock() >= deadline:
                raise CloudFleetBringupError(
                    "timed out waiting for pre-provisioned cloud workers: "
                    + ", ".join(missing_names)
                )
            self._sleep(self._poll_interval_sec)

    def _max_readiness_timeout(
        self,
        adapter: "GceProviderAdapter",
        plan: "CloudLaunchPlan",
    ) -> int:
        return max(
            fleet.readiness_timeout_sec for fleet in adapter.build_worker_fleets(plan)
        )

    def _max_instance_readiness_timeout(
        self,
        adapter: "GceProviderAdapter",
        plan: "CloudLaunchPlan",
    ) -> int:
        timeouts = [
            fleet.readiness_timeout_sec for fleet in adapter.build_worker_fleets(plan)
        ]
        timeouts.extend(
            fleet.readiness_timeout_sec
            for fleet in adapter.build_evaluator_fleets(plan)
        )
        return max(timeouts)

    def wait_for_instances(
        self,
        *,
        experiment_name: str,
        workers: list["GceWorkerRecord"],
        evaluators: list["GceWorkerRecord"],
        timeout_sec: int,
    ) -> CloudFleetSnapshot:
        """Wait until all expected workers and evaluators report `ready`."""
        expected_workers = {
            CloudInstanceRole.WORKER: workers,
            CloudInstanceRole.EVALUATOR: evaluators,
        }
        deadline = self._clock() + timeout_sec

        while True:
            snapshot = self._snapshot_instances(
                experiment_name=experiment_name,
                workers_by_role=expected_workers,
            )
            if snapshot.ready_count == snapshot.requested_count:
                return snapshot
            if snapshot.failed_workers:
                raise CloudFleetBringupError(
                    self._format_failure(
                        "cloud instance bootstrap failed",
                        snapshot,
                    ),
                    snapshot,
                )
            if self._clock() >= deadline:
                raise CloudFleetBringupError(
                    self._format_failure(
                        "timed out waiting for ready instances",
                        snapshot,
                    ),
                    snapshot,
                )
            self._sleep(self._poll_interval_sec)

    def _snapshot_instances(
        self,
        *,
        experiment_name: str,
        workers_by_role: dict[CloudInstanceRole, list["GceWorkerRecord"]],
    ) -> CloudFleetSnapshot:
        expected_instance_ids: list[str] = []
        ready_workers: list[CloudWorkerStatus] = []
        pending_workers: list[CloudWorkerStatus] = []
        failed_workers: list[CloudWorkerStatus] = []
        missing_instance_ids: list[str] = []

        for role, workers in workers_by_role.items():
            if not workers:
                continue
            instance_ids = [worker.instance_id for worker in workers]
            snapshot = self._readiness_store.snapshot(
                experiment_name=experiment_name,
                expected_instance_ids=instance_ids,
                role=role,
            )
            expected_instance_ids.extend(
                f"{role.value}:{instance_id}" for instance_id in instance_ids
            )
            ready_workers.extend(snapshot.ready_workers)
            pending_workers.extend(snapshot.pending_workers)
            failed_workers.extend(snapshot.failed_workers)
            missing_instance_ids.extend(
                f"{role.value}:{instance_id}"
                for instance_id in snapshot.missing_instance_ids
            )

        return CloudFleetSnapshot(
            experiment_name=experiment_name,
            expected_instance_ids=tuple(expected_instance_ids),
            ready_workers=tuple(
                sorted(
                    ready_workers,
                    key=lambda worker: (worker.role, worker.instance_name),
                )
            ),
            pending_workers=tuple(
                sorted(
                    pending_workers,
                    key=lambda worker: (worker.role, worker.instance_name),
                )
            ),
            failed_workers=tuple(
                sorted(
                    failed_workers,
                    key=lambda worker: (worker.role, worker.instance_name),
                )
            ),
            missing_instance_ids=tuple(sorted(missing_instance_ids)),
        )

    def _clear_experiment_roles(
        self,
        *,
        experiment_name: str,
        roles: tuple[CloudInstanceRole, ...],
    ) -> None:
        for role in roles:
            self._readiness_store.clear_experiment(experiment_name, role=role)

    def _teardown_workers(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
        workers: list[GceWorkerRecord],
    ) -> None:
        for worker in workers:
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETING,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleting worker after failed bring-up",
                )
            )

        try:
            deleted_workers = adapter.delete_workers(plan=plan)
        except Exception:
            return
        for worker in deleted_workers:
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETED,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleted after failed bring-up",
                )
            )

    def _teardown_instances(
        self,
        *,
        plan: "CloudLaunchPlan",
        adapter: "GceProviderAdapter",
        workers: list["GceWorkerRecord"],
        evaluators: list["GceWorkerRecord"],
    ) -> None:
        for worker in workers:
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETING,
                    role=CloudInstanceRole.WORKER,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleting worker after failed bring-up",
                )
            )
        for evaluator in evaluators:
            provider_status = coerce_gce_provider_status(evaluator.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=evaluator.instance_id,
                    instance_name=evaluator.name,
                    zone=evaluator.zone,
                    state=CloudWorkerState.DELETING,
                    role=CloudInstanceRole.EVALUATOR,
                    provider_status=provider_status,
                    internal_ip=evaluator.internal_ip,
                    external_ip=evaluator.external_ip,
                    detail="Deleting evaluator after failed bring-up",
                )
            )

        try:
            deleted_workers = adapter.delete_workers(plan=plan)
        except Exception:
            deleted_workers = []
        try:
            deleted_evaluators = adapter.delete_evaluators(plan=plan)
        except Exception:
            deleted_evaluators = []

        for worker in deleted_workers:
            provider_status = coerce_gce_provider_status(worker.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=worker.instance_id,
                    instance_name=worker.name,
                    zone=worker.zone,
                    state=CloudWorkerState.DELETED,
                    role=CloudInstanceRole.WORKER,
                    provider_status=provider_status,
                    internal_ip=worker.internal_ip,
                    external_ip=worker.external_ip,
                    detail="Deleted after failed bring-up",
                )
            )
        for evaluator in deleted_evaluators:
            provider_status = coerce_gce_provider_status(evaluator.status)
            self._readiness_store.record(
                CloudWorkerStatus(
                    experiment_name=plan.experiment_name,
                    instance_id=evaluator.instance_id,
                    instance_name=evaluator.name,
                    zone=evaluator.zone,
                    state=CloudWorkerState.DELETED,
                    role=CloudInstanceRole.EVALUATOR,
                    provider_status=provider_status,
                    internal_ip=evaluator.internal_ip,
                    external_ip=evaluator.external_ip,
                    detail="Deleted after failed bring-up",
                )
            )
