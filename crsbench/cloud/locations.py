"""Provider-dispatched location helpers for shared cloud code."""

from __future__ import annotations

from crsbench.cloud.types import CloudProvider, coerce_cloud_provider


def region_for_provider_zone(
    provider: CloudProvider | str,
    zone: str | None,
) -> str | None:
    """Return the region for a provider-specific zone/location string."""
    if not zone:
        return None

    resolved = coerce_cloud_provider(provider)
    if resolved is CloudProvider.GCE:
        from crsbench.cloud.gce.quota import zone_to_region

        return zone_to_region(zone)

    raise NotImplementedError(
        f"Cloud region derivation is not implemented for provider {resolved.value}"
    )
