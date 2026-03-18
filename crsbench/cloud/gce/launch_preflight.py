"""Operator-side secret resolution for GCE cloud launch inputs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.launch_state import redact_worker_fleet_config
from crsbench.cloud.models import (
    CloudLaunchDefaults,
    CloudLaunchPlan,
    CloudOrchestratorPlan,
    CloudPlacementPlan,
    ResolvedInstanceProfile,
)
from crsbench.cloud.secret_refs import resolve_secret_path, resolve_secret_text


@dataclass(frozen=True)
class GceLaunchPreflight:
    """Resolved launch inputs for provisioning plus redacted persistence copies."""

    resolved_plan: CloudLaunchPlan
    redacted_worker_fleets: list = field(default_factory=list)
    redacted_evaluator_fleets: list = field(default_factory=list)
    orchestrator_env: dict[str, str] = field(default_factory=dict)
    worker_placement_envs: list[dict[str, str]] = field(default_factory=list)
    evaluator_placement_envs: list[dict[str, str]] = field(default_factory=list)


def prepare_gce_launch_inputs(
    *,
    plan: CloudLaunchPlan,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> GceLaunchPreflight:
    """Resolve GCE secret-bearing fields once on the operator before provisioning."""
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    adapter = GceProviderAdapter()
    resolved_plan = _resolve_launch_plan(plan, cwd=base_cwd, env=env)
    _validate_checkout_install_specs_for_plan(resolved_plan)
    resolved_worker_fleets = adapter.build_worker_fleets(resolved_plan)
    resolved_evaluator_fleets = adapter.build_evaluator_fleets(resolved_plan)
    return GceLaunchPreflight(
        resolved_plan=resolved_plan,
        redacted_worker_fleets=[
            redact_worker_fleet_config(fleet) for fleet in resolved_worker_fleets
        ],
        redacted_evaluator_fleets=[
            redact_worker_fleet_config(fleet) for fleet in resolved_evaluator_fleets
        ],
        orchestrator_env=resolved_plan.orchestrator.env,
        worker_placement_envs=[
            placement.env for placement in resolved_plan.worker_placements
        ],
        evaluator_placement_envs=[
            placement.env for placement in resolved_plan.evaluator_placements
        ],
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
        launch_defaults=_resolve_launch_defaults(
            plan.orchestrator.launch_defaults,
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
            launch_defaults=_resolve_launch_defaults(
                placement.launch_defaults,
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
            launch_defaults=_resolve_launch_defaults(
                placement.launch_defaults,
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
    del cwd, env
    return profile


def _resolve_launch_defaults(
    defaults: CloudLaunchDefaults,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> CloudLaunchDefaults:
    return CloudLaunchDefaults(
        readiness_timeout_sec=defaults.readiness_timeout_sec,
        readiness_timeout_sec_field_path=defaults.readiness_timeout_sec_field_path,
        crsbench_install_spec=defaults.crsbench_install_spec,
        crsbench_install_spec_field_path=defaults.crsbench_install_spec_field_path,
        crsbench_git_ref=defaults.crsbench_git_ref,
        crsbench_git_ref_field_path=defaults.crsbench_git_ref_field_path,
        github_deploy_key_file=resolve_secret_path(
            defaults.github_deploy_key_file,
            field_path=defaults.github_deploy_key_file_field_path,
            env=env,
            cwd=cwd,
        ),
        github_deploy_key_file_field_path=defaults.github_deploy_key_file_field_path,
    )


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


def _validate_checkout_install_specs_for_plan(plan: CloudLaunchPlan) -> None:
    _validate_checkout_install_spec(
        plan.orchestrator.launch_defaults.crsbench_install_spec,
        field_path=plan.orchestrator.launch_defaults.crsbench_install_spec_field_path,
    )
    for placement in plan.worker_placements:
        _validate_checkout_install_spec(
            placement.launch_defaults.crsbench_install_spec,
            field_path=placement.launch_defaults.crsbench_install_spec_field_path,
        )
    for placement in plan.evaluator_placements:
        _validate_checkout_install_spec(
            placement.launch_defaults.crsbench_install_spec,
            field_path=placement.launch_defaults.crsbench_install_spec_field_path,
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
