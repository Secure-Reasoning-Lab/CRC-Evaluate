"""Helpers for operator-driven runtime cloud fleet expansion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from crsbench.cloud.gce.launch_preflight import resolve_cloud_env_map
from crsbench.cloud.locations import region_for_provider_zone
from crsbench.cloud.models import (
    _resolve_candidate_regions,
    _resolve_candidate_zones,
    _resolve_fallback_policy,
)
from crsbench.cloud.types import CloudProvider
from crsbench.validation.schemas import _merge_cloud_placement_defaults

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


def _role_defaults_dict(*, config, role: str) -> dict[str, Any]:
    """Return role-level placement defaults for runtime-added fleets."""
    cloud = getattr(config, "cloud", None)
    if cloud is None:
        return {}
    role_container = getattr(
        cloud, "workers" if role == "worker" else "evaluators", None
    )
    defaults = getattr(role_container, "defaults", None)
    if defaults is None:
        return {}
    return defaults.model_dump(exclude_none=True, exclude_unset=True)


def _resolve_dynamic_instance_profile(
    *,
    config,
    role: str,
    merged_placement: dict[str, Any],
) -> str:
    """Resolve the effective instance profile for one runtime-added placement."""
    role_defaults_path = f"cloud.{'workers' if role == 'worker' else 'evaluators'}.defaults.instance_profile"
    instance_profile = str(merged_placement.get("instance_profile") or "").strip()
    if not instance_profile:
        raise ValueError(
            "runtime-added placement instance profile must be provided via "
            f"--instance-profile or {role_defaults_path}"
        )

    providers = getattr(getattr(config, "cloud", None), "providers", None)
    gce = getattr(providers, "gce", None)
    if gce is None or instance_profile not in gce.instance_profiles:
        raise ValueError(
            f"runtime-added placement instance profile '{instance_profile}' was not found"
        )
    return instance_profile


def build_dynamic_placement_request(
    *,
    role: str,
    config,
    instance_profile: str | None,
    count: int | None,
    regions: str | None,
    zones: str | None,
) -> CloudDynamicPlacementRequest:
    """Build and validate one runtime-added placement request."""
    parsed_regions = list(_parse_csv(regions))
    parsed_zones = list(_parse_csv(zones))
    normalized_instance_profile = (
        instance_profile.strip() if isinstance(instance_profile, str) else None
    )
    if normalized_instance_profile == "":
        normalized_instance_profile = None
    effective_count = 1 if count is None else count
    if effective_count <= 0:
        raise ValueError("runtime-added placement count must be greater than zero")

    placement_overrides: dict[str, Any] = {"count": effective_count}
    if normalized_instance_profile is not None:
        placement_overrides["instance_profile"] = normalized_instance_profile
    if parsed_regions:
        placement_overrides["regions"] = parsed_regions
    if parsed_zones:
        placement_overrides["zones"] = parsed_zones
    merged_placement = _merge_cloud_placement_defaults(
        _role_defaults_dict(config=config, role=role),
        placement_overrides,
    )
    effective_regions = tuple(
        _resolve_candidate_regions(
            config=config,
            specific_region=merged_placement.get("region"),
            specific_regions=list(merged_placement.get("regions") or []),
        )
    )
    effective_zones = tuple(
        _resolve_candidate_zones(
            config=config,
            specific_zones=list(merged_placement.get("zones") or []),
        )
    )
    if not effective_regions and not effective_zones:
        raise ValueError(
            "runtime-added placement requires --regions and/or --zones, or "
            "matching role/provider cloud defaults"
        )
    effective_instance_profile = _resolve_dynamic_instance_profile(
        config=config,
        role=role,
        merged_placement=merged_placement,
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
        instance_profile=effective_instance_profile,
        count=effective_count,
        fallback=_resolve_fallback_policy(
            config=config,
            specific_fallback=merged_placement.get("fallback"),
        ),
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
