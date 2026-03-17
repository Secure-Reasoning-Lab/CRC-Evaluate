"""Provisioner boundary for GCE-backed CRSBench worker fleets."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, cast

from crsbench.cloud.gce.metadata import (
    build_instance_metadata,
    build_orchestrator_labels,
    build_orchestrator_metadata,
    build_worker_labels,
    load_orchestrator_startup_script,
    load_startup_script,
)
from crsbench.cloud.gce.models import GceInstanceRequest, GceWorkerRecord

if TYPE_CHECKING:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
    from crsbench.distributed.registry import RuntimeRegistration
    from crsbench.validation.schemas import GceOrchestratorConfig, GceWorkerFleetConfig


class GceProvisioningError(RuntimeError):
    """Raised when the GCE control plane cannot satisfy a fleet request."""


class GceApiClient(Protocol):
    """Minimal client boundary the provisioner needs from GCE."""

    def insert_instance(
        self,
        *,
        project: str,
        zone: str,
        instance_resource: dict[str, object],
    ) -> dict[str, object]: ...

    def wait_for_zone_operation(
        self,
        *,
        project: str,
        zone: str,
        operation: str,
    ) -> dict[str, object]: ...

    def get_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]: ...

    def list_instances(
        self,
        *,
        project: str,
        zone: str,
        label_selector: dict[str, str],
    ) -> list[dict[str, object]]: ...

    def delete_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]: ...


class _InstancesClientProtocol(Protocol):
    def insert(
        self,
        *,
        project: str,
        zone: str,
        instance_resource: dict[str, object],
    ) -> object: ...

    def get(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> object: ...

    def list(
        self,
        request: object | None = None,
        *,
        project: str | None = None,
        zone: str | None = None,
    ) -> Sequence[object]: ...

    def delete(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> object: ...


class _ZoneOperationsClientProtocol(Protocol):
    def wait(
        self,
        *,
        project: str,
        zone: str,
        operation: str,
    ) -> object: ...


class GoogleComputeClient:
    """Lazy wrapper around the Google Compute Engine client library."""

    def __init__(
        self,
        *,
        instances_client: _InstancesClientProtocol | None = None,
        zone_operations_client: _ZoneOperationsClientProtocol | None = None,
    ) -> None:
        self._instances_client = instances_client
        self._zone_operations_client = zone_operations_client

    def insert_instance(
        self,
        *,
        project: str,
        zone: str,
        instance_resource: dict[str, object],
    ) -> dict[str, object]:
        operation = self._instances().insert(
            project=project,
            zone=zone,
            instance_resource=instance_resource,
        )
        return self._coerce_mapping(operation)

    def wait_for_zone_operation(
        self,
        *,
        project: str,
        zone: str,
        operation: str,
    ) -> dict[str, object]:
        result = self._zone_operations().wait(
            project=project,
            zone=zone,
            operation=operation,
        )
        return self._coerce_mapping(result)

    def get_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]:
        result = self._instances().get(project=project, zone=zone, instance=instance)
        return self._coerce_mapping(result)

    def list_instances(
        self,
        *,
        project: str,
        zone: str,
        label_selector: dict[str, str],
    ) -> list[dict[str, object]]:
        return [
            self._coerce_mapping(instance)
            for instance in self._instances().list(
                request={
                    "project": project,
                    "zone": zone,
                    "filter": _build_label_filter(label_selector),
                }
            )
        ]

    def delete_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]:
        operation = self._instances().delete(
            project=project,
            zone=zone,
            instance=instance,
        )
        return self._coerce_mapping(operation)

    def _instances(self) -> _InstancesClientProtocol:
        if self._instances_client is None:
            from google.cloud import compute_v1

            self._instances_client = cast(
                "_InstancesClientProtocol",
                compute_v1.InstancesClient(),
            )
        return self._instances_client

    def _zone_operations(self) -> _ZoneOperationsClientProtocol:
        if self._zone_operations_client is None:
            from google.cloud import compute_v1

            self._zone_operations_client = cast(
                "_ZoneOperationsClientProtocol",
                compute_v1.ZoneOperationsClient(),
            )
        return self._zone_operations_client

    def _coerce_mapping(self, value: object) -> dict[str, object]:
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}

        protobuf_message = getattr(value, "_pb", None)
        if protobuf_message is not None:
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(protobuf_message)

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            result = to_dict()
            if isinstance(result, Mapping):
                return dict(result)

        operation_name = getattr(value, "name", None)
        if isinstance(operation_name, str) and operation_name:
            result: dict[str, object] = {"name": operation_name}
            operation_status = getattr(value, "status", None)
            if isinstance(operation_status, str) and operation_status:
                result["status"] = operation_status
            error_code = getattr(value, "error_code", None)
            error_message = getattr(value, "error_message", None)
            if isinstance(error_code, int) and error_code != 0:
                result["error"] = {
                    "errors": [
                        {
                            "code": str(error_code),
                            "message": str(
                                error_message or "Unknown GCE operation failure"
                            ),
                        }
                    ]
                }
            return result

        raise TypeError(f"Unsupported GCE response type: {type(value)!r}")


_NAME_PATTERN = re.compile(r"[^a-z0-9-]+")


def _sanitize_name_fragment(value: str) -> str:
    cleaned = _NAME_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not cleaned:
        return "worker"
    if not cleaned[0].isalpha():
        cleaned = f"w-{cleaned}"
    return cleaned[:55].rstrip("-") or "worker"


def _extract_operation_name(result: Mapping[str, object]) -> str:
    operation_name = result.get("name")
    if not isinstance(operation_name, str) or not operation_name:
        raise GceProvisioningError(
            f"Missing operation name in provider response: {result}"
        )
    return operation_name


def _extract_operation_errors(result: Mapping[str, object]) -> list[str]:
    error_block = result.get("error")
    if not isinstance(error_block, Mapping):
        return []

    normalized_error_block = {str(key): value for key, value in error_block.items()}
    raw_errors = normalized_error_block.get("errors")
    if not isinstance(raw_errors, Sequence):
        return []

    messages: list[str] = []
    for raw_error in raw_errors:
        if not isinstance(raw_error, Mapping):
            continue
        normalized_error = {str(key): value for key, value in raw_error.items()}
        code = normalized_error.get("code") or "UNKNOWN"
        message = normalized_error.get("message") or "Unknown GCE operation failure"
        messages.append(f"{code}: {message}")
    return messages


def _build_label_filter(label_selector: Mapping[str, str]) -> str:
    parts: list[str] = []
    for key, value in sorted(label_selector.items()):
        escaped_value = value.replace('"', '\\"')
        parts.append(f'(labels.{key} = "{escaped_value}")')
    return " ".join(parts)


def _zone_name_from_self_link(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.rstrip("/").split("/")[-1]


def _normalize_instance(instance: Mapping[str, object]) -> GceWorkerRecord:
    name = str(instance.get("name", ""))
    instance_id = str(instance.get("id", ""))
    status = str(instance.get("status", "UNKNOWN"))
    zone = _zone_name_from_self_link(instance.get("zone"))

    internal_ip = None
    external_ip = None
    network_interfaces = instance.get("networkInterfaces")
    if isinstance(network_interfaces, Sequence) and network_interfaces:
        primary_interface = network_interfaces[0]
        if isinstance(primary_interface, Mapping):
            network_ip = primary_interface.get("networkIP")
            if isinstance(network_ip, str) and network_ip:
                internal_ip = network_ip
            access_configs = primary_interface.get("accessConfigs")
            if isinstance(access_configs, Sequence) and access_configs:
                first_access_config = access_configs[0]
                if isinstance(first_access_config, Mapping):
                    nat_ip = first_access_config.get("natIP")
                    if isinstance(nat_ip, str) and nat_ip:
                        external_ip = nat_ip

    service_account_email = None
    service_accounts = instance.get("serviceAccounts")
    if isinstance(service_accounts, Sequence) and service_accounts:
        first_account = service_accounts[0]
        if isinstance(first_account, Mapping):
            email = first_account.get("email")
            if isinstance(email, str) and email:
                service_account_email = email

    labels = instance.get("labels")
    normalized_labels = dict(labels) if isinstance(labels, Mapping) else {}

    return GceWorkerRecord(
        name=name,
        instance_id=instance_id,
        status=status,
        zone=zone,
        internal_ip=internal_ip,
        external_ip=external_ip,
        service_account_email=service_account_email,
        labels=normalized_labels,
        raw=dict(instance),
    )


class GceProvisioner:
    """Render, create, list, and delete CRSBench GCE worker instances."""

    def __init__(
        self,
        *,
        client: GceApiClient | None = None,
        startup_script: str | None = None,
    ) -> None:
        self._client: GceApiClient = client or GoogleComputeClient()
        self._startup_script = startup_script

    def build_requests(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        redis_host: str,
        redis_password: str | None = None,
        registration: RuntimeRegistration,
        bootstrap_inputs: CloudVmBootstrapInputs | None = None,
    ) -> list[GceInstanceRequest]:
        """Render instance requests from validated config and runtime metadata."""
        zone = self._resolve_zone(fleet)
        labels = build_worker_labels(experiment_name=experiment_name, fleet=fleet)
        startup_script = self._startup_script or load_startup_script()

        requests: list[GceInstanceRequest] = []
        for worker_name in self.build_worker_names(
            experiment_name=experiment_name,
            fleet=fleet,
        ):
            metadata = build_instance_metadata(
                experiment_name=experiment_name,
                fleet=fleet,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                worker_name=worker_name,
                startup_script=startup_script,
            )
            requests.append(
                GceInstanceRequest(
                    project=fleet.project,
                    zone=zone,
                    name=worker_name,
                    labels=dict(labels),
                    metadata=metadata,
                    service_account_email=fleet.service_account_email,
                    ssh_via_iap=fleet.ssh_via_iap,
                    machine_type=fleet.machine_type,
                    boot_disk_size_gb=fleet.boot_disk_size_gb,
                    image=fleet.image,
                    instance_template=fleet.instance_template,
                    network=fleet.network,
                    subnetwork=fleet.subnetwork,
                )
            )
        return requests

    def build_worker_names(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> list[str]:
        """Return deterministic worker instance names for one fleet request."""
        name_prefix = _sanitize_name_fragment(
            fleet.worker_name_prefix or experiment_name or "worker"
        )
        return [
            f"{name_prefix}-{index:03d}" for index in range(1, fleet.worker_count + 1)
        ]

    def create_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        redis_host: str,
        redis_password: str | None = None,
        registration: RuntimeRegistration,
        bootstrap_inputs: CloudVmBootstrapInputs | None = None,
    ) -> list[GceWorkerRecord]:
        """Create a worker fleet and return normalized provider records."""
        requests = self.build_requests(
            experiment_name=experiment_name,
            fleet=fleet,
            redis_host=redis_host,
            redis_password=redis_password,
            registration=registration,
            bootstrap_inputs=bootstrap_inputs,
        )
        workers: list[GceWorkerRecord] = []
        rollback_requests: list[GceInstanceRequest] = []
        try:
            for request in requests:
                operation = self._client.insert_instance(
                    project=request.project,
                    zone=request.zone,
                    instance_resource=request.to_instance_resource(),
                )
                rollback_requests.append(request)
                result = self._client.wait_for_zone_operation(
                    project=request.project,
                    zone=request.zone,
                    operation=_extract_operation_name(operation),
                )
                errors = _extract_operation_errors(result)
                if errors:
                    raise GceProvisioningError(
                        f"Failed to create worker {request.name}: {'; '.join(errors)}"
                    )
                workers.append(
                    _normalize_instance(
                        self._client.get_instance(
                            project=request.project,
                            zone=request.zone,
                            instance=request.name,
                        )
                    )
                )
            return workers
        except Exception:
            self._rollback_requests(rollback_requests)
            raise

    def build_orchestrator_request(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
        experiment_config_path: str,
        redis_password: str,
    ) -> GceInstanceRequest:
        """Render an instance request for the remote orchestrator VM."""
        zone = self._resolve_orchestrator_zone(orchestrator)
        labels = build_orchestrator_labels(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
        )
        startup_script = self._startup_script or load_orchestrator_startup_script()
        instance_name = self._build_orchestrator_name(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
        )
        metadata = build_orchestrator_metadata(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
            experiment_config_path=experiment_config_path,
            redis_password=redis_password,
            startup_script=startup_script,
        )
        return GceInstanceRequest(
            project=orchestrator.project,
            zone=zone,
            name=instance_name,
            labels=labels,
            metadata=metadata,
            service_account_email=orchestrator.service_account_email,
            ssh_via_iap=orchestrator.ssh_via_iap,
            machine_type=orchestrator.machine_type,
            boot_disk_size_gb=orchestrator.boot_disk_size_gb,
            image=orchestrator.image,
            instance_template=orchestrator.instance_template,
            network=orchestrator.network,
            subnetwork=orchestrator.subnetwork,
        )

    def create_orchestrator(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
        experiment_config_path: str,
        redis_password: str,
    ) -> GceWorkerRecord:
        """Create the remote orchestrator VM and return its normalized record."""
        request = self.build_orchestrator_request(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
            experiment_config_path=experiment_config_path,
            redis_password=redis_password,
        )
        try:
            operation = self._client.insert_instance(
                project=request.project,
                zone=request.zone,
                instance_resource=request.to_instance_resource(),
            )
            result = self._client.wait_for_zone_operation(
                project=request.project,
                zone=request.zone,
                operation=_extract_operation_name(operation),
            )
            errors = _extract_operation_errors(result)
            if errors:
                raise GceProvisioningError(
                    f"Failed to create orchestrator {request.name}: {'; '.join(errors)}"
                )
            return _normalize_instance(
                self._client.get_instance(
                    project=request.project,
                    zone=request.zone,
                    instance=request.name,
                )
            )
        except Exception:
            self._rollback_requests([request])
            raise

    def list_orchestrators(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
    ) -> list[GceWorkerRecord]:
        """List orchestrator instances belonging to this experiment."""
        zone = self._resolve_orchestrator_zone(orchestrator)
        expected_labels = build_orchestrator_labels(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
        )
        instances = [
            _normalize_instance(instance)
            for instance in self._client.list_instances(
                project=orchestrator.project,
                zone=zone,
                label_selector=expected_labels,
            )
        ]
        return [
            instance
            for instance in instances
            if all(
                instance.labels.get(key) == value
                for key, value in expected_labels.items()
            )
        ]

    def delete_orchestrators(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
    ) -> list[GceWorkerRecord]:
        """Delete all orchestrator instances owned by this experiment."""
        deleted_instances = self.list_orchestrators(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
        )
        for instance in deleted_instances:
            operation = self._client.delete_instance(
                project=orchestrator.project,
                zone=instance.zone,
                instance=instance.name,
            )
            result = self._client.wait_for_zone_operation(
                project=orchestrator.project,
                zone=instance.zone,
                operation=_extract_operation_name(operation),
            )
            errors = _extract_operation_errors(result)
            if errors:
                raise GceProvisioningError(
                    f"Failed to delete orchestrator {instance.name}: {'; '.join(errors)}"
                )
        return deleted_instances

    def delete_instance(
        self,
        *,
        project: str,
        zone: str,
        instance_name: str,
    ) -> None:
        """Delete one named instance and wait for the zonal operation."""
        operation = self._client.delete_instance(
            project=project,
            zone=zone,
            instance=instance_name,
        )
        result = self._client.wait_for_zone_operation(
            project=project,
            zone=zone,
            operation=_extract_operation_name(operation),
        )
        errors = _extract_operation_errors(result)
        if errors:
            raise GceProvisioningError(
                f"Failed to delete instance {instance_name}: {'; '.join(errors)}"
            )

    def list_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> list[GceWorkerRecord]:
        """List workers belonging to this experiment-scoped fleet."""
        zone = self._resolve_zone(fleet)
        expected_labels = build_worker_labels(
            experiment_name=experiment_name, fleet=fleet
        )
        workers = [
            _normalize_instance(instance)
            for instance in self._client.list_instances(
                project=fleet.project,
                zone=zone,
                label_selector=expected_labels,
            )
        ]
        return [
            worker
            for worker in workers
            if all(
                worker.labels.get(key) == value
                for key, value in expected_labels.items()
            )
        ]

    def delete_workers(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> list[GceWorkerRecord]:
        """Delete all workers owned by this experiment-scoped fleet."""
        deleted_workers = self.list_workers(
            experiment_name=experiment_name,
            fleet=fleet,
        )
        for worker in deleted_workers:
            operation = self._client.delete_instance(
                project=fleet.project,
                zone=worker.zone,
                instance=worker.name,
            )
            result = self._client.wait_for_zone_operation(
                project=fleet.project,
                zone=worker.zone,
                operation=_extract_operation_name(operation),
            )
            errors = _extract_operation_errors(result)
            if errors:
                raise GceProvisioningError(
                    f"Failed to delete worker {worker.name}: {'; '.join(errors)}"
                )
        return deleted_workers

    def _resolve_zone(self, fleet: GceWorkerFleetConfig) -> str:
        if fleet.zone:
            return fleet.zone
        if fleet.region:
            raise GceProvisioningError(
                "cloud.gce.region is not supported yet; configure cloud.gce.zone"
            )
        raise GceProvisioningError("cloud.gce.zone is required for GCE worker fleets")

    def _resolve_orchestrator_zone(self, orchestrator: GceOrchestratorConfig) -> str:
        if orchestrator.zone:
            return orchestrator.zone
        if orchestrator.region:
            raise GceProvisioningError(
                "cloud.orchestrator.region is not supported yet; configure cloud.orchestrator.zone"
            )
        raise GceProvisioningError(
            "cloud.orchestrator.zone is required for GCE orchestrator instances"
        )

    def _build_orchestrator_name(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
    ) -> str:
        prefix = _sanitize_name_fragment(
            orchestrator.instance_name_prefix or "orchestrator"
        )
        experiment_fragment = _sanitize_name_fragment(experiment_name)
        name = f"{prefix}-{experiment_fragment}"
        return name[:63].rstrip("-") or "orchestrator"

    def _rollback_requests(self, requests: list[GceInstanceRequest]) -> None:
        for request in reversed(requests):
            try:
                operation = self._client.delete_instance(
                    project=request.project,
                    zone=request.zone,
                    instance=request.name,
                )
                self._client.wait_for_zone_operation(
                    project=request.project,
                    zone=request.zone,
                    operation=_extract_operation_name(operation),
                )
            except Exception:
                continue
