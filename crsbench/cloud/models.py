"""Provider-neutral cloud launch models and config normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crsbench.cloud.readiness import CloudInstanceRole
from crsbench.cloud.types import CloudProvider
from crsbench.validation.schemas import (
    CloudLaunchDefaultsConfig,
    CloudOrchestratorPlacementConfig,
    ExperimentConfig,
    GceProviderConfig,
)


@dataclass(frozen=True)
class ResolvedInstanceProfile:
    """Resolved provider profile referenced by a launch plan."""

    provider: CloudProvider
    name: str
    provider_config: dict[str, Any]
    profile_config: dict[str, Any]


@dataclass(frozen=True)
class CloudLaunchDefaults:
    """Merged launch/bootstrap defaults for one resolved provider."""

    readiness_timeout_sec: int | None = None
    readiness_timeout_sec_field_path: str = "cloud.defaults.readiness_timeout_sec"
    crsbench_install_spec: str | None = None
    crsbench_install_spec_field_path: str = "cloud.defaults.crsbench_install_spec"
    crsbench_git_ref: str | None = None
    crsbench_git_ref_field_path: str = "cloud.defaults.crsbench_git_ref"
    github_deploy_key_path: str | None = None
    github_deploy_key_path_field_path: str = "cloud.defaults.github_deploy_key_path"


@dataclass(frozen=True)
class CloudOrchestratorPlan:
    """Resolved orchestrator placement for one cloud launch."""

    provider: CloudProvider
    zone: str
    instance_profile: ResolvedInstanceProfile
    launch_defaults: CloudLaunchDefaults = field(default_factory=CloudLaunchDefaults)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudPlacementPlan:
    """Resolved worker/evaluator placement for one cloud launch."""

    role: CloudInstanceRole
    provider: CloudProvider
    zone: str
    count: int
    instance_profile: ResolvedInstanceProfile
    launch_defaults: CloudLaunchDefaults = field(default_factory=CloudLaunchDefaults)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QuotaRequirement:
    """Provider-neutral quota demand record."""

    provider: CloudProvider
    scope: str
    resource_family: str
    required: int


@dataclass(frozen=True)
class QuotaShortage:
    """Provider-neutral quota shortage record."""

    provider: CloudProvider
    scope: str
    resource_family: str
    required: int
    available: int


@dataclass(frozen=True)
class CloudLaunchPlan:
    """Provider-neutral launch plan for one experiment."""

    experiment_name: str
    orchestrator: CloudOrchestratorPlan
    worker_placements: list[CloudPlacementPlan] = field(default_factory=list)
    evaluator_placements: list[CloudPlacementPlan] = field(default_factory=list)


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

    orchestrator_profile = _resolve_provider_profile(
        config=config,
        profile_name=config.cloud.orchestrator.instance_profile,
        field_path="cloud.orchestrator.instance_profile",
    )
    worker_placements = [
        _build_placement_plan(
            config=config,
            role=CloudInstanceRole.WORKER,
            zone=placement.zone or "",
            count=placement.count,
            profile_name=placement.instance_profile,
            placement_env=placement.env,
            field_path=f"cloud.workers.placements.{index}.instance_profile",
        )
        for index, placement in enumerate(config.cloud.workers.placements)
    ]
    evaluator_placements = [
        _build_placement_plan(
            config=config,
            role=CloudInstanceRole.EVALUATOR,
            zone=placement.zone or "",
            count=placement.count,
            profile_name=placement.instance_profile,
            placement_env=placement.env,
            field_path=f"cloud.evaluators.placements.{index}.instance_profile",
        )
        for index, placement in enumerate(
            config.cloud.evaluators.placements if config.cloud.evaluators else []
        )
    ]

    return CloudLaunchPlan(
        experiment_name=config.experiment,
        orchestrator=CloudOrchestratorPlan(
            provider=orchestrator_profile.provider,
            zone=config.cloud.orchestrator.zone,
            instance_profile=orchestrator_profile,
            launch_defaults=_resolve_launch_defaults(
                config=config,
                provider=orchestrator_profile.provider,
            ),
            env=_merge_env_layers(
                config.cloud.env,
                _profile_env(orchestrator_profile),
                config.cloud.orchestrator.env,
            ),
        ),
        worker_placements=worker_placements,
        evaluator_placements=evaluator_placements,
    )


def _resolve_provider_profile(
    *,
    config: ExperimentConfig,
    profile_name: str,
    field_path: str,
) -> ResolvedInstanceProfile:
    """Resolve one named provider profile from the owning provider catalog."""
    matches: list[tuple[CloudProvider, GceProviderConfig]] = []
    providers = config.cloud.providers if config.cloud is not None else None
    if providers is not None and providers.gce is not None:
        if profile_name in providers.gce.instance_profiles:
            matches.append((CloudProvider.GCE, providers.gce))

    if not matches:
        raise ValueError(
            f"{field_path} '{profile_name}' was not found under any "
            "cloud.providers.*.instance_profiles catalog"
        )
    if len(matches) > 1:
        provider_names = ", ".join(provider.value for provider, _ in matches)
        raise ValueError(
            f"{field_path} '{profile_name}' is ambiguous across cloud provider "
            f"catalogs: {provider_names}"
        )

    provider_name, provider_config = matches[0]
    profile = provider_config.instance_profiles[profile_name]
    return ResolvedInstanceProfile(
        provider=provider_name,
        name=profile_name,
        provider_config=provider_config.model_dump(
            exclude_none=True,
            exclude_unset=True,
        ),
        profile_config=profile.model_dump(
            exclude_none=True,
            exclude_unset=True,
        ),
    )


def _build_placement_plan(
    *,
    config: ExperimentConfig,
    role: CloudInstanceRole,
    zone: str,
    count: int,
    profile_name: str,
    placement_env: dict[str, str],
    field_path: str,
) -> CloudPlacementPlan:
    """Build one typed worker/evaluator placement from the provider catalog."""
    instance_profile = _resolve_provider_profile(
        config=config,
        profile_name=profile_name,
        field_path=field_path,
    )
    return CloudPlacementPlan(
        role=role,
        provider=instance_profile.provider,
        zone=zone,
        count=count,
        instance_profile=instance_profile,
        launch_defaults=_resolve_launch_defaults(
            config=config,
            provider=instance_profile.provider,
        ),
        env=_merge_env_layers(
            config.cloud.env if config.cloud is not None else {},
            _profile_env(instance_profile),
            placement_env,
        ),
    )


def _profile_env(profile: ResolvedInstanceProfile) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in profile.profile_config.get("env", {}).items()
    }


def _resolve_launch_defaults(
    *,
    config: ExperimentConfig,
    provider: CloudProvider,
) -> CloudLaunchDefaults:
    cloud = config.cloud
    if cloud is None:
        return CloudLaunchDefaults()

    provider_defaults: CloudLaunchDefaultsConfig | None = None
    if provider is CloudProvider.GCE:
        provider_config = cloud.providers.gce if cloud.providers is not None else None
        provider_defaults = (
            provider_config.defaults if provider_config is not None else None
        )

    return CloudLaunchDefaults(
        readiness_timeout_sec=_get_launch_default_int(
            cloud.defaults,
            provider_defaults,
            "readiness_timeout_sec",
        ),
        readiness_timeout_sec_field_path=_get_launch_default_field_path(
            provider=provider,
            global_defaults=cloud.defaults,
            provider_defaults=provider_defaults,
            field_name="readiness_timeout_sec",
        ),
        crsbench_install_spec=_get_launch_default_str(
            cloud.defaults,
            provider_defaults,
            "crsbench_install_spec",
        ),
        crsbench_install_spec_field_path=_get_launch_default_field_path(
            provider=provider,
            global_defaults=cloud.defaults,
            provider_defaults=provider_defaults,
            field_name="crsbench_install_spec",
        ),
        crsbench_git_ref=_get_launch_default_str(
            cloud.defaults,
            provider_defaults,
            "crsbench_git_ref",
        ),
        crsbench_git_ref_field_path=_get_launch_default_field_path(
            provider=provider,
            global_defaults=cloud.defaults,
            provider_defaults=provider_defaults,
            field_name="crsbench_git_ref",
        ),
        github_deploy_key_path=_get_launch_default_str(
            cloud.defaults,
            provider_defaults,
            "github_deploy_key_path",
        ),
        github_deploy_key_path_field_path=_get_launch_default_field_path(
            provider=provider,
            global_defaults=cloud.defaults,
            provider_defaults=provider_defaults,
            field_name="github_deploy_key_path",
        ),
    )


def _merge_env_layers(*layers: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        merged.update({str(key): str(value) for key, value in layer.items()})
    return merged


def _get_optional_int(mapping: dict[str, Any], field_name: str) -> int | None:
    value = mapping.get(field_name)
    return int(value) if value is not None else None


def _get_optional_str(mapping: dict[str, Any], field_name: str) -> str | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_launch_default_int(
    global_defaults: CloudLaunchDefaultsConfig,
    provider_defaults: CloudLaunchDefaultsConfig | None,
    field_name: str,
) -> int | None:
    provider_value = getattr(provider_defaults, field_name, None)
    if provider_value is not None:
        return int(provider_value)
    global_value = getattr(global_defaults, field_name, None)
    return int(global_value) if global_value is not None else None


def _get_launch_default_str(
    global_defaults: CloudLaunchDefaultsConfig,
    provider_defaults: CloudLaunchDefaultsConfig | None,
    field_name: str,
) -> str | None:
    provider_value = getattr(provider_defaults, field_name, None)
    if provider_value is not None:
        return str(provider_value)
    global_value = getattr(global_defaults, field_name, None)
    return str(global_value) if global_value is not None else None


def _get_launch_default_field_path(
    *,
    provider: CloudProvider,
    global_defaults: CloudLaunchDefaultsConfig,
    provider_defaults: CloudLaunchDefaultsConfig | None,
    field_name: str,
) -> str:
    if getattr(provider_defaults, field_name, None) is not None:
        return f"cloud.providers.{provider.value}.defaults.{field_name}"
    if getattr(global_defaults, field_name, None) is not None:
        return f"cloud.defaults.{field_name}"
    return f"cloud.defaults.{field_name}"
