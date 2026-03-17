"""GCE quota helpers for provider-neutral cloud validation."""

from __future__ import annotations

import re

_MACHINE_TYPE_VCPU_PATTERN = re.compile(r"-(\d+)$")
_QUOTA_METRIC_BY_FAMILY = {
    "a2": "A2_CPUS",
    "c2": "C2_CPUS",
    "c2d": "C2D_CPUS",
    "c3": "C3_CPUS",
    "e2": "E2_CPUS",
    "n2": "N2_CPUS",
    "n2a": "N2A_CPUS",
    "n2d": "N2D_CPUS",
    "t2a": "T2A_CPUS",
    "t2d": "T2D_CPUS",
}


def zone_to_region(zone: str) -> str:
    """Convert a zonal name like ``us-east5-b`` into ``us-east5``."""
    if zone.count("-") < 2:
        raise ValueError(f"Invalid GCE zone: {zone!r}")
    return zone.rsplit("-", 1)[0]


def machine_type_to_family(machine_type: str) -> str:
    """Return the quota family key for a GCE machine type string."""
    normalized = machine_type.strip().lower()
    for family in sorted(_QUOTA_METRIC_BY_FAMILY, key=len, reverse=True):
        prefix = f"{family}-"
        if normalized.startswith(prefix):
            return family
    return "cpus"


def machine_type_to_vcpus(machine_type: str) -> int:
    """Extract the vCPU count from a GCE machine type string."""
    match = _MACHINE_TYPE_VCPU_PATTERN.search(machine_type.strip().lower())
    if match is None:
        raise ValueError(
            f"Could not determine vCPU count from GCE machine type {machine_type!r}"
        )
    return int(match.group(1))


def family_to_quota_metric(resource_family: str) -> str:
    """Map a normalized machine family to the GCE regional quota metric."""
    return _QUOTA_METRIC_BY_FAMILY.get(resource_family, "CPUS")


class GceRegionalQuotaClient:
    """Live GCE regional quota lookups backed by the Compute Regions API."""

    def __init__(self, *, regions_client: object | None = None) -> None:
        self._regions_client = regions_client

    def get_available_capacity(
        self, *, project: str, region: str, resource_family: str
    ) -> int:
        """Return remaining regional capacity for one machine family."""
        metric_name = family_to_quota_metric(resource_family)
        region_resource = self._regions().get(project=project, region=region)
        quotas = getattr(region_resource, "quotas", None)
        if quotas is None and isinstance(region_resource, dict):
            quotas = region_resource.get("quotas", [])
        for quota in quotas or []:
            metric = getattr(quota, "metric", None)
            if metric is None and isinstance(quota, dict):
                metric = quota.get("metric")
            if metric != metric_name:
                continue
            limit = getattr(quota, "limit", None)
            usage = getattr(quota, "usage", None)
            if isinstance(quota, dict):
                limit = quota.get("limit", limit)
                usage = quota.get("usage", usage)
            return int(limit or 0) - int(usage or 0)
        raise KeyError(
            f"Regional quota metric {metric_name!r} not found for {project}:{region}"
        )

    def _regions(self):
        if self._regions_client is None:
            from google.cloud import compute_v1

            self._regions_client = compute_v1.RegionsClient()
        return self._regions_client
