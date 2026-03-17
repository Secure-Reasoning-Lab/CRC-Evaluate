"""Provider-neutral cloud launch models and config normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crsbench.validation.schemas import (
    CloudOrchestratorPlacementConfig,
    ExperimentConfig,
    GceInstanceProfileConfig,
    GceProviderConfig,
)


@dataclass(frozen=True)
class ResolvedInstanceProfile:
    """Resolved provider profile referenced by a launch plan."""

    provider: str
    name: str
    provider_config: dict[str, Any]
    profile_config: dict[str, Any]


@dataclass(frozen=True)
class CloudOrchestratorPlan:
    """Resolved orchestrator placement for one cloud launch."""

    provider: str
    zone: str
    instance_profile: ResolvedInstanceProfile


@dataclass(frozen=True)
class CloudWorkerPlacementPlan:
    """Resolved worker placement for one cloud launch."""

    provider: str
    zone: str
    worker_count: int
    instance_profile: ResolvedInstanceProfile


@dataclass(frozen=True)
class QuotaRequirement:
    """Provider-neutral quota demand record."""

    provider: str
    scope: str
    resource_family: str
    required: int


@dataclass(frozen=True)
class QuotaShortage:
    """Provider-neutral quota shortage record."""

    provider: str
    scope: str
    resource_family: str
    required: int
    available: int


@dataclass(frozen=True)
class CloudLaunchPlan:
    """Provider-neutral launch plan for one experiment."""

    experiment_name: str
    orchestrator: CloudOrchestratorPlan
    worker_placements: list[CloudWorkerPlacementPlan] = field(default_factory=list)


def build_cloud_launch_plan(config: ExperimentConfig) -> CloudLaunchPlan:
    """Build a provider-neutral launch plan from a validated experiment config."""
    if (
        config.cloud is None
        or config.cloud.providers is None
        or config.cloud.providers.gce is None
        or config.cloud.workers is None
        or not isinstance(config.cloud.orchestrator, CloudOrchestratorPlacementConfig)
    ):
        raise ValueError(
            "Experiment config does not define provider-neutral cloud launch config"
        )

    gce_provider = config.cloud.providers.gce
    orchestrator_profile = _resolve_gce_profile(
        provider_name="gce",
        provider_config=gce_provider,
        profile_name=config.cloud.orchestrator.instance_profile,
    )
    worker_placements = [
        CloudWorkerPlacementPlan(
            provider=placement.provider,
            zone=placement.zone or "",
            worker_count=placement.worker_count,
            instance_profile=_resolve_gce_profile(
                provider_name="gce",
                provider_config=gce_provider,
                profile_name=placement.instance_profile,
            ),
        )
        for placement in config.cloud.workers.placements
    ]

    return CloudLaunchPlan(
        experiment_name=config.experiment,
        orchestrator=CloudOrchestratorPlan(
            provider=config.cloud.orchestrator.provider,
            zone=config.cloud.orchestrator.zone,
            instance_profile=orchestrator_profile,
        ),
        worker_placements=worker_placements,
    )


def _resolve_gce_profile(
    *,
    provider_name: str,
    provider_config: GceProviderConfig,
    profile_name: str,
) -> ResolvedInstanceProfile:
    """Resolve one named GCE instance profile into a normalized launch record."""
    profile = provider_config.instance_profiles[profile_name]
    return ResolvedInstanceProfile(
        provider=provider_name,
        name=profile_name,
        provider_config=provider_config.model_dump(),
        profile_config=_profile_dump(profile),
    )


def _profile_dump(profile: GceInstanceProfileConfig) -> dict[str, Any]:
    """Serialize a profile into plain data for provider-neutral plan objects."""
    return profile.model_dump()
