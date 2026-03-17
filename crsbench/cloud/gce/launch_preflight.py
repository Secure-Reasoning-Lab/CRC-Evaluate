"""Operator-side secret resolution for GCE cloud launch inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.launch_state import redact_worker_fleet_config
from crsbench.cloud.models import (
    CloudLaunchPlan,
    CloudOrchestratorPlan,
    CloudWorkerPlacementPlan,
    ResolvedInstanceProfile,
)
from crsbench.cloud.secret_refs import resolve_secret_path, resolve_secret_text

if TYPE_CHECKING:
    from crsbench.validation.schemas import GceOrchestratorConfig, GceWorkerFleetConfig


@dataclass(frozen=True)
class GceLaunchPreflight:
    """Resolved launch inputs for provisioning plus redacted persistence copies."""

    resolved_plan: CloudLaunchPlan | None = None
    resolved_orchestrator: GceOrchestratorConfig | None = None
    resolved_worker_fleets: list[GceWorkerFleetConfig] | None = None
    redacted_worker_fleets: list[GceWorkerFleetConfig] | None = None


def prepare_gce_launch_inputs(
    *,
    plan: CloudLaunchPlan | None = None,
    orchestrator: GceOrchestratorConfig | None = None,
    worker_fleets: Sequence[GceWorkerFleetConfig] | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> GceLaunchPreflight:
    """Resolve GCE secret-bearing fields once on the operator before provisioning."""
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    fleets = list(worker_fleets or [])

    if plan is not None:
        adapter = GceProviderAdapter()
        resolved_plan = _resolve_launch_plan(plan, cwd=base_cwd, env=env)
        resolved_worker_fleets = adapter.build_worker_fleets(resolved_plan)
        return GceLaunchPreflight(
            resolved_plan=resolved_plan,
            resolved_worker_fleets=resolved_worker_fleets,
            redacted_worker_fleets=[
                redact_worker_fleet_config(fleet) for fleet in resolved_worker_fleets
            ],
        )

    resolved_orchestrator = (
        _resolve_orchestrator_config(orchestrator, cwd=base_cwd, env=env)
        if orchestrator is not None
        else None
    )
    resolved_worker_fleets = [
        _resolve_worker_fleet_config(fleet, cwd=base_cwd, env=env) for fleet in fleets
    ]
    return GceLaunchPreflight(
        resolved_orchestrator=resolved_orchestrator,
        resolved_worker_fleets=resolved_worker_fleets,
        redacted_worker_fleets=[
            redact_worker_fleet_config(fleet) for fleet in resolved_worker_fleets
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
    )
    resolved_placements = [
        CloudWorkerPlacementPlan(
            provider=placement.provider,
            zone=placement.zone,
            worker_count=placement.worker_count,
            instance_profile=_resolve_instance_profile(
                placement.instance_profile,
                cwd=cwd,
                env=env,
            ),
        )
        for placement in plan.worker_placements
    ]
    return replace(
        plan,
        orchestrator=resolved_orchestrator,
        worker_placements=resolved_placements,
    )


def _resolve_instance_profile(
    profile: ResolvedInstanceProfile,
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> ResolvedInstanceProfile:
    profile_config = dict(profile.profile_config)
    field_prefix = f"cloud.providers.gce.instance_profiles.{profile.name}"
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
