"""Provisioner boundary for GCE-backed CRSBench worker fleets."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, cast

from crsbench.cloud.gce.metadata import (
    build_evaluator_labels,
    build_evaluator_metadata,
    build_instance_metadata,
    build_orchestrator_labels,
    build_orchestrator_metadata,
    build_worker_labels,
    load_evaluator_startup_script,
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
        source_instance_template: str | None = None,
    ) -> dict[str, object]: ...

    def wait_for_zone_operation(
        self,
        *,
        project: str,
        zone: str,
        operation: str,
    ) -> dict[str, object]: ...

    def bulk_insert_instances(
        self,
        *,
        project: str,
        region: str,
        bulk_insert_instance_resource: dict[str, object],
        source_instance_template: str | None = None,
    ) -> dict[str, object]: ...

    def wait_for_region_operation(
        self,
        *,
        project: str,
        region: str,
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

    def list_instances_in_region(
        self,
        *,
        project: str,
        region: str,
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
        request: object | None = None,
        **kwargs,
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

    def aggregated_list(
        self,
        request: object | None = None,
        **kwargs,
    ) -> Sequence[tuple[str, object]]: ...

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


class _RegionInstancesClientProtocol(Protocol):
    def bulk_insert(
        self,
        request: object | None = None,
        **kwargs,
    ) -> object: ...


class _RegionOperationsClientProtocol(Protocol):
    def wait(
        self,
        *,
        project: str,
        region: str,
        operation: str,
    ) -> object: ...


class GoogleComputeClient:
    """Lazy wrapper around the Google Compute Engine client library."""

    def __init__(
        self,
        *,
        instances_client: _InstancesClientProtocol | None = None,
        zone_operations_client: _ZoneOperationsClientProtocol | None = None,
        region_instances_client: _RegionInstancesClientProtocol | None = None,
        region_operations_client: _RegionOperationsClientProtocol | None = None,
    ) -> None:
        self._instances_client = instances_client
        self._zone_operations_client = zone_operations_client
        self._region_instances_client = region_instances_client
        self._region_operations_client = region_operations_client

    def insert_instance(
        self,
        *,
        project: str,
        zone: str,
        instance_resource: dict[str, object],
        source_instance_template: str | None = None,
    ) -> dict[str, object]:
        from google.cloud import compute_v1

        request_kwargs: dict[str, object] = {
            "project": project,
            "zone": zone,
            "instance_resource": compute_v1.Instance(instance_resource),
        }
        if source_instance_template:
            request_kwargs["source_instance_template"] = source_instance_template

        request = compute_v1.InsertInstanceRequest(
            request_kwargs,
        )
        operation = self._instances().insert(
            request=request,
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

    def bulk_insert_instances(
        self,
        *,
        project: str,
        region: str,
        bulk_insert_instance_resource: dict[str, object],
        source_instance_template: str | None = None,
    ) -> dict[str, object]:
        from google.cloud import compute_v1

        resource_kwargs = dict(bulk_insert_instance_resource)
        if source_instance_template:
            resource_kwargs["source_instance_template"] = source_instance_template
        request = compute_v1.BulkInsertRegionInstanceRequest(
            {
                "project": project,
                "region": region,
                "bulk_insert_instance_resource_resource": compute_v1.BulkInsertInstanceResource(
                    resource_kwargs
                ),
            }
        )
        operation = self._region_instances().bulk_insert(request=request)
        return self._coerce_mapping(operation)

    def wait_for_region_operation(
        self,
        *,
        project: str,
        region: str,
        operation: str,
    ) -> dict[str, object]:
        result = self._region_operations().wait(
            project=project,
            region=region,
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

    def list_instances_in_region(
        self,
        *,
        project: str,
        region: str,
        label_selector: dict[str, str],
    ) -> list[dict[str, object]]:
        instances: list[dict[str, object]] = []
        for scope, scoped_list in self._instances().aggregated_list(
            request={
                "project": project,
                "filter": _build_label_filter(label_selector),
            }
        ):
            zone = _zone_name_from_self_link(scope)
            if not zone or ("/zones/" not in scope and not scope.startswith("zones/")):
                continue
            if not zone.startswith(f"{region}-"):
                continue
            scoped_mapping = self._coerce_mapping(scoped_list)
            raw_instances = scoped_mapping.get("instances")
            if not isinstance(raw_instances, Sequence):
                continue
            for instance in raw_instances:
                instances.append(self._coerce_mapping(instance))
        return instances

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

    def _region_instances(self) -> _RegionInstancesClientProtocol:
        if self._region_instances_client is None:
            from google.cloud import compute_v1

            self._region_instances_client = cast(
                "_RegionInstancesClientProtocol",
                compute_v1.RegionInstancesClient(),
            )
        return self._region_instances_client

    def _region_operations(self) -> _RegionOperationsClientProtocol:
        if self._region_operations_client is None:
            from google.cloud import compute_v1

            self._region_operations_client = cast(
                "_RegionOperationsClientProtocol",
                compute_v1.RegionOperationsClient(),
            )
        return self._region_operations_client

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
_GCE_INSTANCE_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")


def _sanitize_name_fragment(value: str) -> str:
    cleaned = _NAME_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not cleaned:
        return "worker"
    if not cleaned[0].isalpha():
        cleaned = f"w-{cleaned}"
    return cleaned


def _validate_gce_instance_name(name: str) -> str:
    if _GCE_INSTANCE_NAME_PATTERN.fullmatch(name):
        return name
    raise GceProvisioningError(
        "Generated GCE instance name is invalid; expected 1-63 chars matching "
        "^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ "
        f"but got {name!r} (length={len(name)})"
    )


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


def _is_retryable_zonal_capacity_error(exc: Exception) -> bool:
    if not isinstance(exc, GceProvisioningError):
        return False
    message = str(exc)
    return "ZONE_RESOURCE_POOL_EXHAUSTED" in message


def _build_label_filter(label_selector: Mapping[str, str]) -> str:
    parts: list[str] = []
    for key, value in sorted(label_selector.items()):
        escaped_value = value.replace('"', '\\"')
        parts.append(f'(labels.{key} = "{escaped_value}")')
    return " ".join(parts)


def _require_request_zone(request: GceInstanceRequest) -> str:
    if request.zone is None:
        raise GceProvisioningError(
            f"Missing zone for zonal request {request.name}; expected zonal path only"
        )
    return request.zone


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
        zone: str | None = None,
        region_based: bool = False,
        redis_host: str,
        redis_password: str | None = None,
        registration: RuntimeRegistration,
        bootstrap_inputs: CloudVmBootstrapInputs | None = None,
        env_passthrough: dict[str, str] | None = None,
    ) -> list[GceInstanceRequest]:
        """Render instance requests from validated config and runtime metadata."""
        if zone is None and not region_based:
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
                env_passthrough=env_passthrough,
                worker_name=None if region_based else worker_name,
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
                    assign_external_ip=fleet.assign_external_ip,
                    machine_type=fleet.machine_type,
                    boot_disk_size_gb=fleet.boot_disk_size_gb,
                    image=fleet.image,
                    instance_template=fleet.instance_template,
                    network=fleet.network,
                    subnetwork=fleet.subnetwork,
                )
            )
        return requests

    def build_evaluator_requests(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        zone: str | None = None,
        region_based: bool = False,
        redis_host: str,
        registration: RuntimeRegistration,
        experiment_config_path: str,
        redis_password: str | None = None,
        bootstrap_inputs: CloudVmBootstrapInputs | None = None,
        env_passthrough: dict[str, str] | None = None,
    ) -> list[GceInstanceRequest]:
        """Render evaluator instance requests from validated config and runtime metadata."""
        if zone is None and not region_based:
            zone = self._resolve_zone(fleet)
        labels = build_evaluator_labels(experiment_name=experiment_name, fleet=fleet)
        startup_script = self._startup_script or load_evaluator_startup_script()

        requests: list[GceInstanceRequest] = []
        for evaluator_name in self.build_worker_names(
            experiment_name=experiment_name,
            fleet=fleet,
        ):
            metadata = build_evaluator_metadata(
                experiment_name=experiment_name,
                fleet=fleet,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
                evaluator_name=None if region_based else evaluator_name,
                experiment_config_path=experiment_config_path,
                startup_script=startup_script,
            )
            requests.append(
                GceInstanceRequest(
                    project=fleet.project,
                    zone=zone,
                    name=evaluator_name,
                    labels=dict(labels),
                    metadata=metadata,
                    service_account_email=fleet.service_account_email,
                    ssh_via_iap=fleet.ssh_via_iap,
                    assign_external_ip=fleet.assign_external_ip,
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
        start_index = fleet.worker_name_start_index
        return [
            _validate_gce_instance_name(f"{name_prefix}-{index:03d}")
            for index in range(start_index, start_index + fleet.worker_count)
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
        env_passthrough: dict[str, str] | None = None,
    ) -> list[GceWorkerRecord]:
        """Create a worker fleet and return normalized provider records."""
        if fleet.region is not None:
            return self._create_requests_in_region(
                region=fleet.region,
                zones=list(fleet.zones),
                request_builder=lambda: self.build_requests(
                    experiment_name=experiment_name,
                    fleet=fleet,
                    zone=fleet.zones[0] if fleet.zones else None,
                    region_based=True,
                    redis_host=redis_host,
                    redis_password=redis_password,
                    registration=registration,
                    bootstrap_inputs=bootstrap_inputs,
                    env_passthrough=env_passthrough,
                ),
                role_label="worker",
                label_selector=build_worker_labels(
                    experiment_name=experiment_name,
                    fleet=fleet,
                ),
            )
        return self._create_requests_with_zone_fallback(
            zones=self._candidate_zones_for_fleet(fleet),
            fallback=fleet.fallback,
            request_builder=lambda zone: self.build_requests(
                experiment_name=experiment_name,
                fleet=fleet,
                zone=zone,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
            ),
            role_label="worker",
        )

    def create_evaluators(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
        redis_host: str,
        registration: RuntimeRegistration,
        experiment_config_path: str,
        redis_password: str | None = None,
        bootstrap_inputs: CloudVmBootstrapInputs | None = None,
        env_passthrough: dict[str, str] | None = None,
    ) -> list[GceWorkerRecord]:
        """Create an evaluator fleet and return normalized provider records."""
        if fleet.region is not None:
            return self._create_requests_in_region(
                region=fleet.region,
                zones=list(fleet.zones),
                request_builder=lambda: self.build_evaluator_requests(
                    experiment_name=experiment_name,
                    fleet=fleet,
                    zone=fleet.zones[0] if fleet.zones else None,
                    region_based=True,
                    redis_host=redis_host,
                    redis_password=redis_password,
                    registration=registration,
                    experiment_config_path=experiment_config_path,
                    bootstrap_inputs=bootstrap_inputs,
                    env_passthrough=env_passthrough,
                ),
                role_label="evaluator",
                label_selector=build_evaluator_labels(
                    experiment_name=experiment_name,
                    fleet=fleet,
                ),
            )
        return self._create_requests_with_zone_fallback(
            zones=self._candidate_zones_for_fleet(fleet),
            fallback=fleet.fallback,
            request_builder=lambda zone: self.build_evaluator_requests(
                experiment_name=experiment_name,
                fleet=fleet,
                zone=zone,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                experiment_config_path=experiment_config_path,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
            ),
            role_label="evaluator",
        )

    def build_orchestrator_request(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
        zone: str | None = None,
        experiment_config_path: str,
        env_passthrough: dict[str, str] | None = None,
        redis_password: str,
    ) -> GceInstanceRequest:
        """Render an instance request for the remote orchestrator VM."""
        if zone is None and orchestrator.region is None:
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
            env_passthrough=env_passthrough,
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
            assign_external_ip=orchestrator.assign_external_ip,
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
        env_passthrough: dict[str, str] | None = None,
        redis_password: str,
    ) -> GceWorkerRecord:
        """Create the remote orchestrator VM and return its normalized record."""
        if orchestrator.region is not None:
            requests = self._create_requests_in_region(
                region=orchestrator.region,
                zones=list(orchestrator.zones),
                request_builder=lambda: [
                    self.build_orchestrator_request(
                        experiment_name=experiment_name,
                        orchestrator=orchestrator,
                        zone=orchestrator.zones[0] if orchestrator.zones else None,
                        experiment_config_path=experiment_config_path,
                        env_passthrough=env_passthrough,
                        redis_password=redis_password,
                    )
                ],
                role_label="orchestrator",
                label_selector=build_orchestrator_labels(
                    experiment_name=experiment_name,
                    orchestrator=orchestrator,
                ),
            )
            return requests[0]
        requests = self._create_requests_with_zone_fallback(
            zones=self._candidate_zones_for_orchestrator(orchestrator),
            fallback=orchestrator.fallback,
            request_builder=lambda zone: [
                self.build_orchestrator_request(
                    experiment_name=experiment_name,
                    orchestrator=orchestrator,
                    zone=zone,
                    experiment_config_path=experiment_config_path,
                    env_passthrough=env_passthrough,
                    redis_password=redis_password,
                )
            ],
            role_label="orchestrator",
        )
        return requests[0]

    def list_orchestrators(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
    ) -> list[GceWorkerRecord]:
        """List orchestrator instances belonging to this experiment."""
        expected_labels = build_orchestrator_labels(
            experiment_name=experiment_name,
            orchestrator=orchestrator,
        )
        if orchestrator.region is not None:
            return self._list_records_in_region(
                project=orchestrator.project,
                region=orchestrator.region,
                label_selector=expected_labels,
                allowed_zones=orchestrator.zones,
            )
        instances: dict[tuple[str, str], GceWorkerRecord] = {}
        for zone in self._candidate_zones_for_orchestrator(orchestrator):
            zone_instances = [
                _normalize_instance(instance)
                for instance in self._client.list_instances(
                    project=orchestrator.project,
                    zone=zone,
                    label_selector=expected_labels,
                )
            ]
            for instance in zone_instances:
                if all(
                    instance.labels.get(key) == value
                    for key, value in expected_labels.items()
                ):
                    instances[(instance.zone, instance.name)] = instance
        return list(instances.values())

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
        expected_labels = build_worker_labels(
            experiment_name=experiment_name, fleet=fleet
        )
        if fleet.region is not None:
            return self._list_records_in_region(
                project=fleet.project,
                region=fleet.region,
                label_selector=expected_labels,
                allowed_zones=fleet.zones,
            )
        workers: dict[tuple[str, str], GceWorkerRecord] = {}
        for zone in self._candidate_zones_for_fleet(fleet):
            zone_workers = [
                _normalize_instance(instance)
                for instance in self._client.list_instances(
                    project=fleet.project,
                    zone=zone,
                    label_selector=expected_labels,
                )
            ]
            for worker in zone_workers:
                if all(
                    worker.labels.get(key) == value
                    for key, value in expected_labels.items()
                ):
                    workers[(worker.zone, worker.name)] = worker
        return list(workers.values())

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

    def list_evaluators(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> list[GceWorkerRecord]:
        """List evaluators belonging to this experiment-scoped fleet."""
        expected_labels = build_evaluator_labels(
            experiment_name=experiment_name,
            fleet=fleet,
        )
        if fleet.region is not None:
            return self._list_records_in_region(
                project=fleet.project,
                region=fleet.region,
                label_selector=expected_labels,
                allowed_zones=fleet.zones,
            )
        workers: dict[tuple[str, str], GceWorkerRecord] = {}
        for zone in self._candidate_zones_for_fleet(fleet):
            zone_workers = [
                _normalize_instance(instance)
                for instance in self._client.list_instances(
                    project=fleet.project,
                    zone=zone,
                    label_selector=expected_labels,
                )
            ]
            for worker in zone_workers:
                if all(
                    worker.labels.get(key) == value
                    for key, value in expected_labels.items()
                ):
                    workers[(worker.zone, worker.name)] = worker
        return list(workers.values())

    def delete_evaluators(
        self,
        *,
        experiment_name: str,
        fleet: GceWorkerFleetConfig,
    ) -> list[GceWorkerRecord]:
        """Delete all evaluators owned by this experiment-scoped fleet."""
        deleted_workers = self.list_evaluators(
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
                    f"Failed to delete evaluator {worker.name}: {'; '.join(errors)}"
                )
        return deleted_workers

    def _create_requests_with_zone_fallback(
        self,
        *,
        zones: list[str],
        fallback: bool,
        request_builder: Callable[[str], Sequence[GceInstanceRequest]],
        role_label: str,
    ) -> list[GceWorkerRecord]:
        last_exc: GceProvisioningError | None = None
        for index, zone in enumerate(zones):
            try:
                return self._create_request_group(
                    request_builder(zone),
                    role_label=role_label,
                )
            except GceProvisioningError as exc:
                last_exc = exc
                should_retry = (
                    fallback
                    and index < len(zones) - 1
                    and _is_retryable_zonal_capacity_error(exc)
                )
                if not should_retry:
                    raise
        assert last_exc is not None
        raise last_exc

    def _create_requests_in_region(
        self,
        *,
        region: str,
        zones: list[str],
        request_builder: Callable[[], Sequence[GceInstanceRequest]],
        role_label: str,
        label_selector: Mapping[str, str],
    ) -> list[GceWorkerRecord]:
        requests = list(request_builder())
        if not requests:
            return []

        bulk_request = self._build_bulk_insert_request(
            requests=requests,
            allowed_zones=zones,
        )
        project = requests[0].project
        rollback_workers: list[GceWorkerRecord] = []
        try:
            operation = self._client.bulk_insert_instances(
                project=project,
                region=region,
                bulk_insert_instance_resource=bulk_request,
                source_instance_template=requests[0].instance_template,
            )
            result = self._client.wait_for_region_operation(
                project=project,
                region=region,
                operation=_extract_operation_name(operation),
            )
            errors = _extract_operation_errors(result)
            if errors:
                raise GceProvisioningError(
                    f"Failed to create {role_label} fleet in region {region}: "
                    f"{'; '.join(errors)}"
                )

            workers_by_name = {
                worker.name: worker
                for worker in self._list_records_in_region(
                    project=project,
                    region=region,
                    label_selector=label_selector,
                    allowed_zones=zones,
                )
            }
            ordered_workers: list[GceWorkerRecord] = []
            for request in requests:
                worker = workers_by_name.get(request.name)
                if worker is None:
                    rollback_workers = list(workers_by_name.values())
                    raise GceProvisioningError(
                        f"Regional bulk insert did not return {role_label} "
                        f"{request.name} in region {region}"
                    )
                ordered_workers.append(worker)
            return ordered_workers
        except Exception:
            self._rollback_records(project=project, workers=rollback_workers)
            raise

    def _create_request_group(
        self,
        requests: Sequence[GceInstanceRequest],
        *,
        role_label: str,
    ) -> list[GceWorkerRecord]:
        workers: list[GceWorkerRecord] = []
        rollback_requests: list[GceInstanceRequest] = []
        try:
            for request in requests:
                zone = _require_request_zone(request)
                operation = self._client.insert_instance(
                    project=request.project,
                    zone=zone,
                    instance_resource=request.to_instance_resource(),
                    source_instance_template=request.instance_template,
                )
                rollback_requests.append(request)
                result = self._client.wait_for_zone_operation(
                    project=request.project,
                    zone=zone,
                    operation=_extract_operation_name(operation),
                )
                errors = _extract_operation_errors(result)
                if errors:
                    raise GceProvisioningError(
                        f"Failed to create {role_label} {request.name}: {'; '.join(errors)}"
                    )
                workers.append(
                    _normalize_instance(
                        self._client.get_instance(
                            project=request.project,
                            zone=zone,
                            instance=request.name,
                        )
                    )
                )
            return workers
        except Exception:
            self._rollback_requests(rollback_requests)
            raise

    def _candidate_zones_for_fleet(self, fleet: GceWorkerFleetConfig) -> list[str]:
        if fleet.zones:
            return list(fleet.zones)
        return [self._resolve_zone(fleet)]

    def _candidate_zones_for_orchestrator(
        self,
        orchestrator: GceOrchestratorConfig,
    ) -> list[str]:
        if orchestrator.zones:
            return list(orchestrator.zones)
        return [self._resolve_orchestrator_zone(orchestrator)]

    def _resolve_zone(self, fleet: GceWorkerFleetConfig) -> str:
        if fleet.zone:
            return fleet.zone
        if fleet.region:
            raise GceProvisioningError(
                "cloud.gce.zone resolution is unavailable for regional placements"
            )
        raise GceProvisioningError(
            "cloud.gce requires zone, zones, or region for worker placement"
        )

    def _resolve_orchestrator_zone(self, orchestrator: GceOrchestratorConfig) -> str:
        if orchestrator.zone:
            return orchestrator.zone
        if orchestrator.region:
            raise GceProvisioningError(
                "cloud.orchestrator.zone resolution is unavailable for regional placements"
            )
        raise GceProvisioningError(
            "cloud.orchestrator requires zone, zones, or region for placement"
        )

    def _build_orchestrator_name(
        self,
        *,
        experiment_name: str,
        orchestrator: GceOrchestratorConfig,
    ) -> str:
        experiment_fragment = _sanitize_name_fragment(experiment_name)
        if orchestrator.instance_name_prefix:
            prefix = _sanitize_name_fragment(orchestrator.instance_name_prefix)
            name = f"{prefix}-{experiment_fragment}"
        else:
            name = f"crsbench-{experiment_fragment}-orch"
        return _validate_gce_instance_name(name)

    def _rollback_requests(self, requests: list[GceInstanceRequest]) -> None:
        for request in reversed(requests):
            try:
                zone = _require_request_zone(request)
                operation = self._client.delete_instance(
                    project=request.project,
                    zone=zone,
                    instance=request.name,
                )
                self._client.wait_for_zone_operation(
                    project=request.project,
                    zone=zone,
                    operation=_extract_operation_name(operation),
                )
            except Exception:
                continue

    def _rollback_records(
        self,
        *,
        project: str,
        workers: Sequence[GceWorkerRecord],
    ) -> None:
        for worker in reversed(list(workers)):
            try:
                self.delete_instance(
                    project=project,
                    zone=worker.zone,
                    instance_name=worker.name,
                )
            except Exception:
                continue

    def _build_bulk_insert_request(
        self,
        *,
        requests: Sequence[GceInstanceRequest],
        allowed_zones: Sequence[str],
    ) -> dict[str, object]:
        if not requests:
            raise GceProvisioningError("Expected at least one regional request")
        shared_properties = requests[0].to_bulk_insert_instance_properties()
        for request in requests[1:]:
            if (
                request.project != requests[0].project
                or request.instance_template != requests[0].instance_template
                or request.to_bulk_insert_instance_properties() != shared_properties
            ):
                raise GceProvisioningError(
                    "Regional bulk insert requires identical shared instance properties"
                )

        resource: dict[str, object] = {
            "count": len(requests),
            "min_count": len(requests),
            "instance_properties": shared_properties,
            "per_instance_properties": {request.name: {} for request in requests},
        }
        location_policy: dict[str, object] = {"target_shape": "ANY_SINGLE_ZONE"}
        if allowed_zones:
            location_policy["zones"] = [
                {"zone": f"zones/{zone}"} for zone in allowed_zones
            ]
        resource["location_policy"] = location_policy
        return resource

    def _list_records_in_region(
        self,
        *,
        project: str,
        region: str,
        label_selector: Mapping[str, str],
        allowed_zones: Sequence[str],
    ) -> list[GceWorkerRecord]:
        allowed_zone_set = set(allowed_zones)
        records: dict[tuple[str, str], GceWorkerRecord] = {}
        for instance in self._client.list_instances_in_region(
            project=project,
            region=region,
            label_selector=dict(label_selector),
        ):
            record = _normalize_instance(instance)
            if allowed_zone_set and record.zone not in allowed_zone_set:
                continue
            if all(
                record.labels.get(key) == value for key, value in label_selector.items()
            ):
                records[(record.zone, record.name)] = record
        return list(records.values())
