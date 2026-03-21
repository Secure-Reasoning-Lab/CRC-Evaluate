"""Provider-neutral cloud runtime and persisted-state records."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from crsbench.cloud.locations import region_for_provider_zone
from crsbench.cloud.types import CloudProvider, coerce_cloud_provider


class CloudInstanceRecord(BaseModel):
    """Provider-neutral cloud instance record used by shared operator paths."""

    model_config = ConfigDict(extra="forbid")

    provider: CloudProvider
    role: str
    name: str
    instance_id: str
    status: str
    project: str | None = None
    zone: str | None = None
    region: str | None = None
    internal_ip: str | None = None
    external_ip: str | None = None
    ssh_via_iap: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class CloudInstanceLike(Protocol):
    """Minimal shared live-instance shape used by generic cloud operations."""

    name: str
    instance_id: str
    status: str
    zone: str
    internal_ip: str | None
    external_ip: str | None
    labels: dict[str, str]


class CloudFleetPlacementRecord(BaseModel):
    """Provider-neutral persisted placement record for one launched fleet slot."""

    model_config = ConfigDict(extra="forbid")

    provider: CloudProvider
    role: str
    project: str
    zone: str | None = None
    zones: list[str] = Field(default_factory=list)
    region: str | None = None
    regions: list[str] = Field(default_factory=list)
    count: int
    name_prefix: str
    name_start_index: int = 1
    ssh_via_iap: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    owner_label: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


def cloud_instance_record_from_legacy_gce_dict(
    value: dict[str, Any],
    *,
    role: str,
    project: str | None = None,
    ssh_via_iap: bool = False,
) -> CloudInstanceRecord:
    """Translate legacy persisted GCE orchestrator fields into a neutral record."""
    zone = value.get("zone")
    provider = coerce_cloud_provider(value.get("provider", CloudProvider.GCE))
    return CloudInstanceRecord(
        provider=provider,
        role=role,
        name=str(value["name"]),
        instance_id=str(value.get("instance_id", f"{role}:{value['name']}")),
        status=str(value.get("status", "RUNNING")),
        project=project,
        zone=zone,
        region=region_for_provider_zone(provider, zone)
        if isinstance(zone, str) and zone
        else None,
        internal_ip=value.get("internal_ip"),
        external_ip=value.get("external_ip"),
        ssh_via_iap=ssh_via_iap,
        labels={
            str(key): str(map_value)
            for key, map_value in value.get("labels", {}).items()
        },
        provider_metadata=dict(value.get("provider_metadata", {})),
    )


def cloud_fleet_placement_record_from_legacy_gce_dict(
    value: dict[str, Any],
    *,
    role: str,
) -> CloudFleetPlacementRecord:
    """Translate persisted legacy GCE fleet config fields into a neutral record."""
    zones = [str(zone) for zone in value.get("zones", [])]
    regions = [str(region) for region in value.get("regions", [])]
    region = value.get("region")
    zone = value.get("zone")
    provider = coerce_cloud_provider(value.get("provider", CloudProvider.GCE))
    if region is None and zones:
        region = region_for_provider_zone(provider, zones[0])
    return CloudFleetPlacementRecord(
        provider=provider,
        role=role,
        project=str(value["project"]),
        zone=str(zone) if zone is not None else None,
        zones=zones,
        region=str(region) if region is not None else None,
        regions=regions,
        count=int(value.get("count", value.get("worker_count", 0))),
        name_prefix=str(value.get("name_prefix", value.get("worker_name_prefix", ""))),
        name_start_index=int(
            value.get("name_start_index", value.get("worker_name_start_index", 1))
        ),
        ssh_via_iap=bool(value.get("ssh_via_iap", False)),
        labels={
            str(key): str(map_value)
            for key, map_value in value.get("labels", {}).items()
        },
        owner_label=(
            str(value["owner_label"]) if value.get("owner_label") is not None else None
        ),
        provider_metadata=dict(value),
    )
