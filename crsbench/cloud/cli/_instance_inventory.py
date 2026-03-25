"""Shared live-instance inventory helpers for cloud operational commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from crsbench.cloud.locations import region_for_provider_zone
from crsbench.cloud.providers import provider_adapter_for_context

if TYPE_CHECKING:
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext
    from crsbench.cloud.records import CloudInstanceLike


@dataclass(frozen=True)
class CloudInstanceInventoryRow:
    """Resolved live cloud instance row for CLI list/ssh operations."""

    alias: str
    name: str
    role: str
    placement_source: str
    provider: str
    project: str
    zone: str
    region: str
    status: str
    internal_ip: str | None
    external_ip: str | None
    ssh_via_iap: bool

    def as_dict(self) -> dict[str, str | None | bool]:
        """Return a JSON-serializable view of the inventory row."""
        return asdict(self)


def list_cloud_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner,
) -> list[CloudInstanceInventoryRow]:
    """Return live orchestrator/worker/evaluator rows for one experiment."""
    rows: list[CloudInstanceInventoryRow] = []
    launch_state = context.launch_state
    if launch_state is not None:
        try:
            orchestrator = provisioner.get_instance_record(
                project=launch_state.orchestrator_project,
                zone=launch_state.orchestrator_zone,
                instance_name=launch_state.orchestrator_name,
            )
        except Exception:
            orchestrator = None
        if orchestrator is not None:
            rows.append(
                CloudInstanceInventoryRow(
                    alias="orch",
                    name=orchestrator.name,
                    role="orchestrator",
                    placement_source="config",
                    provider=launch_state.orchestrator_provider.value,
                    project=launch_state.orchestrator_project,
                    zone=orchestrator.zone,
                    region=(
                        getattr(orchestrator, "region", None)
                        or region_for_provider_zone(
                            launch_state.orchestrator_provider,
                            orchestrator.zone,
                        )
                        or ""
                    ),
                    status=orchestrator.status,
                    internal_ip=orchestrator.internal_ip,
                    external_ip=orchestrator.external_ip,
                    ssh_via_iap=launch_state.orchestrator_ssh_via_iap,
                )
            )

    for instance in list_live_instances(context, experiment_name, provisioner):
        fleet = resolve_instance_fleet(context, instance)
        role = instance.labels.get("crsbench-role", "worker")
        rows.append(
            CloudInstanceInventoryRow(
                alias=_instance_alias(experiment_name, instance.name, role),
                name=instance.name,
                role=role,
                placement_source=getattr(fleet, "placement_source", "config"),
                provider=fleet.provider.value,
                project=fleet.project,
                zone=instance.zone,
                region=region_for_provider_zone(fleet.provider, instance.zone) or "",
                status=instance.status,
                internal_ip=instance.internal_ip,
                external_ip=instance.external_ip,
                ssh_via_iap=bool(getattr(fleet, "ssh_via_iap", False)),
            )
        )

    return sorted(rows, key=_inventory_sort_key)


def list_live_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner,
) -> list[CloudInstanceLike]:
    """List live worker/evaluator instances for the current cloud context."""
    adapter = provider_adapter_for_context(context, provisioner=provisioner)
    if context.launch_state is not None:
        workers: list[CloudInstanceLike] = []
        for fleet in context.worker_fleet_configs:
            workers.extend(
                provisioner.list_workers(
                    experiment_name=experiment_name,
                    fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
                )
            )
        for fleet in context.evaluator_fleet_configs:
            workers.extend(
                provisioner.list_evaluators(
                    experiment_name=experiment_name,
                    fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
                )
            )
        return _dedupe_instances(workers)

    if context.launch_plan is not None:
        workers = adapter.list_workers(plan=context.launch_plan)
        if context.evaluator_fleet_configs:
            workers.extend(adapter.list_evaluators(plan=context.launch_plan))
        return _dedupe_instances(workers)

    workers = []
    for fleet in context.worker_fleet_configs:
        workers.extend(
            provisioner.list_workers(
                experiment_name=experiment_name,
                fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
            )
        )
    for fleet in context.evaluator_fleet_configs:
        workers.extend(
            provisioner.list_evaluators(
                experiment_name=experiment_name,
                fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
            )
        )
    return _dedupe_instances(workers)


def resolve_instance_fleet(
    context: "ResolvedCloudContext",
    worker: CloudInstanceLike,
):
    """Resolve the fleet config that owns one live worker/evaluator VM."""
    role = worker.labels.get("crsbench-role", "worker")
    return resolve_instance_fleet_record(
        context,
        instance_name=worker.name,
        zone=worker.zone,
        role=role,
    )


def resolve_instance_fleet_record(
    context: "ResolvedCloudContext",
    *,
    instance_name: str,
    zone: str,
    role: str,
):
    """Resolve the fleet config that owns one live or persisted instance row."""
    candidate_fleets = (
        context.evaluator_fleet_configs
        if role == "evaluator"
        else context.worker_fleet_configs
    )
    name_matches = [
        fleet
        for fleet in candidate_fleets
        if _fleet_matches_instance_name(fleet, instance_name)
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    zone_filtered_name_matches = [
        fleet for fleet in name_matches if _fleet_targets_zone(fleet, zone)
    ]
    if zone_filtered_name_matches:
        return zone_filtered_name_matches[0]

    zone_matches = [
        fleet for fleet in candidate_fleets if _fleet_targets_zone(fleet, zone)
    ]
    if zone_matches:
        return zone_matches[0]

    raise RuntimeError(
        f"No cloud fleet config matched instance {instance_name} in zone {zone}"
    )


def resolve_inventory_selector(
    rows: list[CloudInstanceInventoryRow],
    selector: str,
) -> CloudInstanceInventoryRow | None:
    """Resolve an inventory selector against alias or full instance name."""
    exact_alias_matches = [row for row in rows if row.alias == selector]
    if len(exact_alias_matches) == 1:
        return exact_alias_matches[0]
    exact_name_matches = [row for row in rows if row.name == selector]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    return None


def _instance_alias(experiment_name: str, instance_name: str, role: str) -> str:
    prefix = f"crsbench-{experiment_name}-"
    if instance_name.startswith(prefix):
        return instance_name.removeprefix(prefix)
    if role == "orchestrator":
        return "orch"
    return instance_name


def _inventory_sort_key(row: CloudInstanceInventoryRow) -> tuple[int, str]:
    role_order = {
        "orchestrator": 0,
        "worker": 1,
        "evaluator": 2,
    }
    return (role_order.get(row.role, 99), row.name)


def _fleet_matches_instance_name(fleet, instance_name: str) -> bool:
    prefix = getattr(fleet, "name_prefix", getattr(fleet, "worker_name_prefix", None))
    if not isinstance(prefix, str) or not prefix:
        return False
    if not instance_name.startswith(f"{prefix}-"):
        return False
    suffix = instance_name.removeprefix(f"{prefix}-")
    if not suffix.isdigit():
        return False
    index = int(suffix)
    start = getattr(
        fleet,
        "name_start_index",
        getattr(fleet, "worker_name_start_index", 1),
    )
    count = getattr(fleet, "count", getattr(fleet, "worker_count", 0))
    return start <= index < start + count


def _fleet_targets_zone(fleet, zone: str) -> bool:
    if getattr(fleet, "zones", None):
        return zone in fleet.zones
    return fleet.zone == zone


def _dedupe_instances(
    instances: list[CloudInstanceLike],
) -> list[CloudInstanceLike]:
    deduped: dict[tuple[str, str, str], CloudInstanceLike] = {}
    for instance in instances:
        role = instance.labels.get("crsbench-role", "worker")
        deduped[(role, instance.zone, instance.name)] = instance
    return list(deduped.values())
