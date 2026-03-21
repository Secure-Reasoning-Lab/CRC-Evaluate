"""Provider resolution helpers for shared cloud CLI/runtime code."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.cloud.types import (
    CloudProvider,
    coerce_cloud_provider,
    validate_single_cloud_provider,
)

if TYPE_CHECKING:
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext
    from crsbench.cloud.models import CloudLaunchPlan


def provider_for_launch_plan(plan: "CloudLaunchPlan") -> CloudProvider:
    """Resolve the single cloud provider required by one launch plan."""
    return validate_single_cloud_provider(
        [
            plan.orchestrator.provider,
            *(placement.provider for placement in plan.worker_placements),
            *(placement.provider for placement in plan.evaluator_placements),
        ]
    )


def provider_for_context(context: "ResolvedCloudContext") -> CloudProvider:
    """Resolve the single cloud provider for one operational cloud context."""
    if context.launch_state is not None:
        return context.launch_state.orchestrator_provider
    if context.launch_plan is not None:
        return provider_for_launch_plan(context.launch_plan)
    raise ValueError("Cloud context does not contain launch state or a launch plan")


def provisioner_for_provider(provider: CloudProvider | str):
    """Return the provider-owned provisioner implementation."""
    resolved = coerce_cloud_provider(provider)
    if resolved is CloudProvider.GCE:
        return GceProvisioner()
    raise NotImplementedError(f"Cloud provisioner is not implemented for {provider}")


def provisioner_for_context(context: "ResolvedCloudContext"):
    """Return the provider-owned provisioner for one operational context."""
    return provisioner_for_provider(provider_for_context(context))


def provider_adapter_for_provider(
    provider: CloudProvider | str,
    *,
    provisioner=None,
):
    """Return the provider-neutral adapter backed by the provider implementation."""
    resolved = coerce_cloud_provider(provider)
    if resolved is CloudProvider.GCE:
        return GceProviderAdapter(provisioner=provisioner or GceProvisioner())
    raise NotImplementedError(
        f"Cloud provider adapter is not implemented for {provider}"
    )


def provider_adapter_for_context(
    context: "ResolvedCloudContext",
    *,
    provisioner=None,
):
    """Return the provider-neutral adapter for one operational cloud context."""
    return provider_adapter_for_provider(
        provider_for_context(context),
        provisioner=provisioner,
    )
