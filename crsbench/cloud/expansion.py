"""Helpers for operator-driven runtime cloud fleet expansion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from crsbench.cloud.gce.launch_preflight import resolve_cloud_env_map
from crsbench.cloud.locations import region_for_provider_zone
from crsbench.cloud.types import CloudProvider

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class CloudDynamicPlacementRequest:
    """Normalized operator request for one runtime-added cloud placement."""

    role: str
    provider: CloudProvider
    instance_profile: str
    count: int
    fallback: bool = True
    regions: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_dynamic_placement_request(
    *,
    role: str,
    config,
    instance_profile: str,
    count: int,
    regions: str | None,
    zones: str | None,
) -> CloudDynamicPlacementRequest:
    """Build and validate one runtime-added placement request."""
    effective_regions = _parse_csv(regions)
    effective_zones = _parse_csv(zones)
    if not effective_regions and not effective_zones:
        raise ValueError("runtime-added placement requires --regions and/or --zones")
    if count <= 0:
        raise ValueError("runtime-added placement count must be greater than zero")

    providers = getattr(getattr(config, "cloud", None), "providers", None)
    gce = getattr(providers, "gce", None)
    if gce is None or instance_profile not in gce.instance_profiles:
        raise ValueError(
            f"runtime-added placement instance profile '{instance_profile}' was not found"
        )

    if effective_regions and effective_zones:
        invalid_zones = [
            zone
            for zone in effective_zones
            if region_for_provider_zone(CloudProvider.GCE, zone)
            not in effective_regions
        ]
        if invalid_zones:
            joined = ", ".join(invalid_zones)
            raise ValueError(
                f"runtime-added placement zones must belong to declared regions: {joined}"
            )

    return CloudDynamicPlacementRequest(
        role=role,
        provider=CloudProvider.GCE,
        instance_profile=instance_profile,
        count=count,
        fallback=bool(getattr(gce, "fallback", True)),
        regions=effective_regions,
        zones=effective_zones,
    )


def next_name_start_index(fleet_records: Sequence[object], *, role: str) -> int:
    """Return the next deterministic start index for one fleet role."""
    max_index = 0
    for record in fleet_records:
        record_role = getattr(record, "role", None)
        if record_role != role:
            continue
        count = int(getattr(record, "count", 0) or 0)
        start_index = int(getattr(record, "name_start_index", 1) or 1)
        max_index = max(max_index, start_index + max(count - 1, 0))
    return max_index + 1 if max_index else 1


def resolve_dynamic_placement_env(
    *,
    config,
    role: str,
    instance_profile: str,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve inherited env for one runtime-added placement."""
    cloud = getattr(config, "cloud", None)
    shared_env = getattr(cloud, "env", None) or {}
    providers = getattr(cloud, "providers", None)
    gce = getattr(providers, "gce", None)
    if gce is None or instance_profile not in gce.instance_profiles:
        raise ValueError(
            f"runtime-added placement instance profile '{instance_profile}' was not found"
        )
    role_container = getattr(
        cloud, "workers" if role == "worker" else "evaluators", None
    )
    role_defaults_env = (
        getattr(getattr(role_container, "defaults", None), "env", None) or {}
    )
    profile_env = getattr(gce.instance_profiles[instance_profile], "env", None) or {}
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    resolved_shared_env = resolve_cloud_env_map(
        shared_env,
        field_prefix="cloud.env",
        cwd=base_cwd,
        env=env,
    )
    resolved_profile_env = resolve_cloud_env_map(
        profile_env,
        field_prefix=f"cloud.providers.gce.instance_profiles.{instance_profile}.env",
        cwd=base_cwd,
        env=env,
    )
    resolved_role_defaults_env = resolve_cloud_env_map(
        role_defaults_env,
        field_prefix=f"cloud.{'workers' if role == 'worker' else 'evaluators'}.defaults.env",
        cwd=base_cwd,
        env=env,
    )
    return {
        **resolved_shared_env,
        **resolved_profile_env,
        **resolved_role_defaults_env,
    }
