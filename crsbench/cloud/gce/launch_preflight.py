"""Operator-side secret resolution for GCE cloud launch inputs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from crsbench.cloud.env_passthrough import (
    merge_env_passthrough,
    resolve_env_passthrough,
)
from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.launch_state import redact_worker_fleet_config
from crsbench.cloud.models import (
    CloudLaunchPlan,
    CloudOrchestratorPlan,
    CloudPlacementPlan,
    ResolvedInstanceProfile,
)
from crsbench.cloud.secret_refs import resolve_secret_path, resolve_secret_text

if TYPE_CHECKING:
    from crsbench.validation.schemas import (
        CloudBootstrapConfig,
        GceOrchestratorConfig,
        GceWorkerFleetConfig,
    )


@dataclass(frozen=True)
class GceLaunchPreflight:
    """Resolved launch inputs for provisioning plus redacted persistence copies."""

    resolved_plan: CloudLaunchPlan | None = None
    resolved_orchestrator: GceOrchestratorConfig | None = None
    resolved_worker_fleets: list[GceWorkerFleetConfig] | None = None
    redacted_worker_fleets: list[GceWorkerFleetConfig] | None = None
    resolved_evaluator_fleets: list[GceWorkerFleetConfig] | None = None
    redacted_evaluator_fleets: list[GceWorkerFleetConfig] | None = None
    orchestrator_env: dict[str, str] = field(default_factory=dict)
    worker_env: dict[str, str] = field(default_factory=dict)
    evaluator_env: dict[str, str] = field(default_factory=dict)
    worker_placement_envs: list[dict[str, str]] = field(default_factory=list)
    evaluator_placement_envs: list[dict[str, str]] = field(default_factory=list)


def prepare_gce_launch_inputs(
    *,
    plan: CloudLaunchPlan | None = None,
    orchestrator: GceOrchestratorConfig | None = None,
    worker_fleets: Sequence[GceWorkerFleetConfig] | None = None,
    bootstrap: CloudBootstrapConfig | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> GceLaunchPreflight:
    """Resolve GCE secret-bearing fields once on the operator before provisioning."""
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    fleets = list(worker_fleets or [])
    orchestrator_env, worker_env, evaluator_env = _resolve_env_passthrough(
        bootstrap=bootstrap,
        env=env,
    )

    if plan is not None:
        adapter = GceProviderAdapter()
        resolved_plan = _resolve_launch_plan(plan, cwd=base_cwd, env=env)
        _validate_checkout_install_specs_for_plan(resolved_plan)
        resolved_worker_fleets = adapter.build_worker_fleets(resolved_plan)
        resolved_evaluator_fleets = adapter.build_evaluator_fleets(resolved_plan)
        resolved_orchestrator_env = {
            **orchestrator_env,
            **resolved_plan.orchestrator.env,
        }
        resolved_worker_envs = [
            {
                **worker_env,
                **placement.env,
            }
            for placement in resolved_plan.worker_placements
        ]
        resolved_evaluator_envs = [
            {
                **evaluator_env,
                **placement.env,
            }
            for placement in resolved_plan.evaluator_placements
        ]
        return GceLaunchPreflight(
            resolved_plan=resolved_plan,
            resolved_worker_fleets=resolved_worker_fleets,
            resolved_evaluator_fleets=resolved_evaluator_fleets,
            redacted_worker_fleets=[
                redact_worker_fleet_config(fleet) for fleet in resolved_worker_fleets
            ],
            redacted_evaluator_fleets=[
                redact_worker_fleet_config(fleet) for fleet in resolved_evaluator_fleets
            ],
            orchestrator_env=resolved_orchestrator_env,
            worker_env=worker_env,
            evaluator_env=evaluator_env,
            worker_placement_envs=resolved_worker_envs,
            evaluator_placement_envs=resolved_evaluator_envs,
        )

    resolved_orchestrator = (
        _resolve_orchestrator_config(orchestrator, cwd=base_cwd, env=env)
        if orchestrator is not None
        else None
    )
    resolved_worker_fleets = [
        _resolve_worker_fleet_config(fleet, cwd=base_cwd, env=env) for fleet in fleets
    ]
    _validate_checkout_install_specs_for_legacy_configs(
        resolved_orchestrator=resolved_orchestrator,
        resolved_worker_fleets=resolved_worker_fleets,
    )
    return GceLaunchPreflight(
        resolved_orchestrator=resolved_orchestrator,
        resolved_worker_fleets=resolved_worker_fleets,
        redacted_worker_fleets=[
            redact_worker_fleet_config(fleet) for fleet in resolved_worker_fleets
        ],
        orchestrator_env=orchestrator_env,
        worker_env=worker_env,
        evaluator_env=evaluator_env,
    )


def _resolve_env_passthrough(
    *,
    bootstrap: CloudBootstrapConfig | None,
    env: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if bootstrap is None:
        return {}, {}, {}

    common = list(bootstrap.env_passthrough.common)
    orchestrator_names = merge_env_passthrough(
        common,
        bootstrap.env_passthrough.orchestrator,
    )
    worker_names = merge_env_passthrough(common, bootstrap.env_passthrough.workers)
    evaluator_names = merge_env_passthrough(
        common,
        bootstrap.env_passthrough.evaluators,
    )
    return (
        resolve_env_passthrough(
            orchestrator_names,
            field_path="cloud.bootstrap.env_passthrough.orchestrator",
            env=env,
        ),
        resolve_env_passthrough(
            worker_names,
            field_path="cloud.bootstrap.env_passthrough.workers",
            env=env,
        ),
        resolve_env_passthrough(
            evaluator_names,
            field_path="cloud.bootstrap.env_passthrough.evaluators",
            env=env,
        ),
    )


def _resolve_launch_plan(
    plan: CloudLaunchPlan,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> CloudLaunchPlan:
    resolved_orchestrator = CloudOrchestratorPlan(
        provider=plan.orchestrator.provider,
        zone=plan.orchestrator.zone,
        instance_profile=_resolve_instance_profile(
            plan.orchestrator.instance_profile,
            cwd=cwd,
            env=env,
        ),
        env=_resolve_cloud_env_map(
            plan.orchestrator.env,
            field_prefix="cloud.orchestrator.env",
            cwd=cwd,
            env=env,
        ),
    )
    resolved_placements = [
        CloudPlacementPlan(
            role=placement.role,
            provider=placement.provider,
            zone=placement.zone,
            count=placement.count,
            instance_profile=_resolve_instance_profile(
                placement.instance_profile,
                cwd=cwd,
                env=env,
            ),
            env=_resolve_cloud_env_map(
                placement.env,
                field_prefix=f"cloud.workers.placements.{index}.env",
                cwd=cwd,
                env=env,
            ),
        )
        for index, placement in enumerate(plan.worker_placements)
    ]
    resolved_evaluator_placements = [
        CloudPlacementPlan(
            role=placement.role,
            provider=placement.provider,
            zone=placement.zone,
            count=placement.count,
            instance_profile=_resolve_instance_profile(
                placement.instance_profile,
                cwd=cwd,
                env=env,
            ),
            env=_resolve_cloud_env_map(
                placement.env,
                field_prefix=f"cloud.evaluators.placements.{index}.env",
                cwd=cwd,
                env=env,
            ),
        )
        for index, placement in enumerate(plan.evaluator_placements)
    ]
    return replace(
        plan,
        orchestrator=resolved_orchestrator,
        worker_placements=resolved_placements,
        evaluator_placements=resolved_evaluator_placements,
    )


def _resolve_instance_profile(
    profile: ResolvedInstanceProfile,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> ResolvedInstanceProfile:
    profile_config = dict(profile.profile_config)
    field_prefix = _profile_field_prefix(profile)
    profile_config["github_deploy_key_file"] = resolve_secret_path(
        profile_config.get("github_deploy_key_file"),
        field_path=f"{field_prefix}.github_deploy_key_file",
        env=env,
        cwd=cwd,
    )
    profile_config["hf_token"] = resolve_secret_text(
        profile_config.get("hf_token"),
        field_path=f"{field_prefix}.hf_token",
        env=env,
        cwd=cwd,
    )
    return replace(profile, profile_config=profile_config)


def _resolve_cloud_env_map(
    values: Mapping[str, str],
    *,
    field_prefix: str,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> dict[str, str]:
    return {
        str(key): str(
            resolve_secret_text(
                value,
                field_path=f"{field_prefix}.{key}",
                env=env,
                cwd=cwd,
            )
        )
        for key, value in values.items()
    }


def _profile_field_prefix(profile: ResolvedInstanceProfile) -> str:
    """Return the config field prefix for one resolved provider profile."""
    return f"cloud.providers.{profile.provider.value}.instance_profiles.{profile.name}"


def _resolve_orchestrator_config(
    orchestrator: GceOrchestratorConfig,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> GceOrchestratorConfig:
    return orchestrator.model_copy(
        update={
            "github_deploy_key_file": resolve_secret_path(
                orchestrator.github_deploy_key_file,
                field_path="cloud.orchestrator.github_deploy_key_file",
                env=env,
                cwd=cwd,
            ),
            "hf_token": resolve_secret_text(
                orchestrator.hf_token,
                field_path="cloud.orchestrator.hf_token",
                env=env,
                cwd=cwd,
            ),
        }
    )


def _resolve_worker_fleet_config(
    fleet: GceWorkerFleetConfig,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> GceWorkerFleetConfig:
    field_prefix = f"cloud.gce[{fleet.zone}]"
    return fleet.model_copy(
        update={
            "github_deploy_key_file": resolve_secret_path(
                fleet.github_deploy_key_file,
                field_path=f"{field_prefix}.github_deploy_key_file",
                env=env,
                cwd=cwd,
            ),
            "hf_token": resolve_secret_text(
                fleet.hf_token,
                field_path=f"{field_prefix}.hf_token",
                env=env,
                cwd=cwd,
            ),
        }
    )


def _validate_checkout_install_specs_for_plan(plan: CloudLaunchPlan) -> None:
    _validate_checkout_install_spec(
        plan.orchestrator.instance_profile.profile_config.get("crsbench_install_spec"),
        field_path=(
            f"{_profile_field_prefix(plan.orchestrator.instance_profile)}."
            "crsbench_install_spec"
        ),
    )
    for placement in plan.worker_placements:
        _validate_checkout_install_spec(
            placement.instance_profile.profile_config.get("crsbench_install_spec"),
            field_path=(
                f"{_profile_field_prefix(placement.instance_profile)}."
                "crsbench_install_spec"
            ),
        )
    for placement in plan.evaluator_placements:
        _validate_checkout_install_spec(
            placement.instance_profile.profile_config.get("crsbench_install_spec"),
            field_path=(
                f"{_profile_field_prefix(placement.instance_profile)}."
                "crsbench_install_spec"
            ),
        )


def _validate_checkout_install_specs_for_legacy_configs(
    *,
    resolved_orchestrator: GceOrchestratorConfig | None,
    resolved_worker_fleets: Sequence[GceWorkerFleetConfig],
) -> None:
    if resolved_orchestrator is not None:
        _validate_checkout_install_spec(
            resolved_orchestrator.crsbench_install_spec,
            field_path="cloud.orchestrator.crsbench_install_spec",
        )
    for fleet in resolved_worker_fleets:
        _validate_checkout_install_spec(
            fleet.crsbench_install_spec,
            field_path=f"cloud.gce[{fleet.zone}].crsbench_install_spec",
        )


def _validate_checkout_install_spec(value: object, *, field_path: str) -> None:
    if value is None or not str(value).strip():
        raise ValueError(
            f"{field_path} is required for cloud VM bootstrap and must use a git+ checkout install spec"
        )
    install_spec = str(value).strip()
    if not install_spec.startswith("git+"):
        raise ValueError(
            f"{field_path} must use a git+ checkout install spec for cloud VM bootstrap, got {install_spec!r}"
        )
