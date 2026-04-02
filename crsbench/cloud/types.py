"""Shared cloud type definitions used across validation and runtime code."""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable, Literal, TypeAlias

CloudProviderName: TypeAlias = Literal["gce"]


class CloudProvider(StrEnum):
    """Known cloud providers in CRSBench's internal type system."""

    GCE = "gce"
    AWS = "aws"
    AZURE = "azure"


class CloudProviderInstanceStatus(StrEnum):
    """Typed instance-status values accepted by shared cloud status code."""

    PROVISIONING = "PROVISIONING"
    STAGING = "STAGING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    SUSPENDING = "SUSPENDING"
    SUSPENDED = "SUSPENDED"
    REPAIRING = "REPAIRING"
    TERMINATED = "TERMINATED"


def coerce_cloud_provider(value: CloudProvider | str) -> CloudProvider:
    """Normalize a provider string into the internal enum."""
    if isinstance(value, CloudProvider):
        return value
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("Cloud provider is required")
    try:
        return CloudProvider(normalized)
    except ValueError as exc:
        raise ValueError(f"Unsupported cloud provider: {value}") from exc


def validate_single_cloud_provider(
    providers: Iterable[CloudProvider | str],
) -> CloudProvider:
    """Require that one launch resolves to exactly one cloud provider."""
    normalized = {coerce_cloud_provider(provider) for provider in providers}
    if not normalized:
        raise ValueError("Cloud launch requires at least one provider")
    if len(normalized) != 1:
        provider_list = ", ".join(provider.value for provider in sorted(normalized))
        raise ValueError(
            "Cross-provider cloud launches are not supported yet; "
            f"found providers: {provider_list}"
        )
    return next(iter(normalized))


def coerce_gce_provider_status(
    value: CloudProviderInstanceStatus | str,
) -> CloudProviderInstanceStatus:
    """Normalize raw GCE instance status strings into the shared enum."""
    if isinstance(value, CloudProviderInstanceStatus):
        return value
    normalized = value.strip().upper()
    try:
        return CloudProviderInstanceStatus(normalized)
    except ValueError as exc:
        raise ValueError(f"Unsupported GCE instance status: {value}") from exc
