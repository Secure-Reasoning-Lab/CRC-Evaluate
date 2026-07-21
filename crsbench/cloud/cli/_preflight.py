"""Read-only launch readiness report for provider-neutral cloud configs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from crsbench.cloud.cli._config_reconnect import load_experiment_config
from crsbench.cloud.launch_checks import find_launch_target_conflicts
from crsbench.cloud.models import CloudLaunchPlan, build_cloud_launch_plan
from crsbench.cloud.preflight_report import (
    CloudPreflightCheck,
    CloudPreflightCheckStatus,
    CloudPreflightReport,
    build_cloud_preflight_report,
)
from crsbench.cloud.providers import (
    prepare_launch_inputs,
    provider_adapter_for_launch_plan,
)
from crsbench.cloud.quota import QuotaValidator
from crsbench.utils.litellm_config import read_internal_litellm_config_snapshot
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.validation.schemas import ExperimentConfig


logger = get_logger(__name__)
_RUNTIME_MANAGED_ENV_KEYS = [
    "CRSBENCH_REDIS_HOST",
    "CRSBENCH_REDIS_PASSWORD",
]


def run_preflight(args: argparse.Namespace) -> int:
    """Print a read-only launch readiness report without provisioning anything."""
    config_path = Path(args.config)
    try:
        config = load_experiment_config(config_path)
    except Exception as exc:
        logger.error("Cloud preflight failed: {}", exc)
        return 2

    if config.cloud is None:
        logger.error("Experiment config must define cloud configuration for preflight")
        return 2

    try:
        read_internal_litellm_config_snapshot(config_path)
    except Exception as exc:
        logger.error("Cloud preflight failed: {}", exc)
        return 2

    try:
        launch_plan = build_cloud_launch_plan(config)
    except ValueError as exc:
        logger.error("Cloud preflight failed: {}", exc)
        return 2

    provider = launch_plan.orchestrator.provider.value

    try:
        adapter = provider_adapter_for_launch_plan(launch_plan)
    except Exception as exc:
        report = _build_report(
            config=config,
            provider=provider,
            plan=launch_plan,
            checks=[
                CloudPreflightCheck(
                    name="provider_adapter",
                    status=CloudPreflightCheckStatus.FAIL,
                    summary="Cloud provider adapter is unavailable for this launch plan.",
                    detail=str(exc),
                )
            ],
        )
        return _emit_report(report, strict=args.strict, json_output=args.json_output)

    try:
        conflicts = find_launch_target_conflicts(
            config_path=config_path,
            experiment_name=config.experiment,
            adapter=adapter,
            plan=launch_plan,
        )
    except Exception as exc:
        return _emit_report(
            _build_report(
                config=config,
                provider=provider,
                plan=launch_plan,
                checks=[
                    CloudPreflightCheck(
                        name="duplicate_launch_guard",
                        status=CloudPreflightCheckStatus.FAIL,
                        summary="Cloud launch target could not be inspected.",
                        detail=str(exc),
                    )
                ],
            ),
            strict=args.strict,
            json_output=args.json_output,
        )
    if conflicts:
        report = _build_report(
            config=config,
            provider=provider,
            plan=launch_plan,
            checks=[
                CloudPreflightCheck(
                    name="duplicate_launch_guard",
                    status=CloudPreflightCheckStatus.FAIL,
                    summary="Cloud launch target is not clear.",
                    detail="; ".join(conflicts),
                )
            ],
        )
        return _emit_report(report, strict=args.strict, json_output=args.json_output)

    checks = [
        CloudPreflightCheck(
            name="duplicate_launch_guard",
            status=CloudPreflightCheckStatus.PASS,
            summary="No saved launch state or live fleet conflict detected.",
        )
    ]

    try:
        preflight = prepare_launch_inputs(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
    except Exception as exc:
        report = _build_report(
            config=config,
            provider=provider,
            plan=launch_plan,
            checks=[
                *checks,
                CloudPreflightCheck(
                    name="provider_preflight",
                    status=CloudPreflightCheckStatus.FAIL,
                    summary="Provider launch-input preflight failed.",
                    detail=str(exc),
                ),
            ],
        )
        return _emit_report(report, strict=args.strict, json_output=args.json_output)

    checks.append(
        CloudPreflightCheck(
            name="provider_preflight",
            status=CloudPreflightCheckStatus.PASS,
            summary="Provider launch-input preflight completed successfully.",
        )
    )

    try:
        QuotaValidator(adapters={provider: adapter}).validate(launch_plan)
    except Exception as exc:
        report = _build_report(
            config=config,
            provider=provider,
            plan=preflight.resolved_plan,
            checks=[
                *checks,
                CloudPreflightCheck(
                    name="quota",
                    status=CloudPreflightCheckStatus.FAIL,
                    summary="Provider quota checks failed for this launch plan.",
                    detail=str(exc),
                ),
            ],
        )
        return _emit_report(report, strict=args.strict, json_output=args.json_output)

    checks.append(
        CloudPreflightCheck(
            name="quota",
            status=CloudPreflightCheckStatus.PASS,
            summary="Provider quota is sufficient for the requested launch plan.",
        )
    )

    remote_root_warning = _remote_root_warning_check(config)
    if remote_root_warning is not None:
        checks.append(remote_root_warning)

    report = _build_report(
        config=config,
        provider=provider,
        plan=preflight.resolved_plan,
        checks=checks,
    )
    return _emit_report(report, strict=args.strict, json_output=args.json_output)


def _build_report(
    *,
    config: "ExperimentConfig",
    provider: str,
    plan: CloudLaunchPlan | object,
    checks: list[CloudPreflightCheck],
) -> CloudPreflightReport:
    return build_cloud_preflight_report(
        experiment=config.experiment,
        provider=provider,
        plan=_plan_summary(plan),
        resolved_defaults=_resolved_defaults(config, plan),
        env_summary=_env_summary(config, provider, plan),
        checks=checks,
        reconnect_notes=[
            "status, events, and monitor require control-plane reachability.",
            "collect and teardown can fall back to persisted launch state plus provider inventory after a successful launch.",
        ],
    )


def _emit_report(
    report: CloudPreflightReport,
    *,
    strict: bool,
    json_output: bool,
) -> int:
    if json_output:
        sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
    else:
        _print_human_report(report, strict=strict)
    if report.verdict == "warning" and strict:
        return 1
    if report.verdict == "blocked":
        return 1
    return 0


def _print_human_report(report: CloudPreflightReport, *, strict: bool) -> None:
    lines = [
        "Summary",
        f"  Experiment: {report.experiment}",
        f"  Provider: {report.provider}",
        f"  Verdict: {report.verdict}",
        "Plan",
    ]
    _append_json_section(lines, report.plan)
    lines.extend(
        [
            "Defaults",
        ]
    )
    _append_json_section(lines, report.resolved_defaults)
    lines.extend(
        [
            "Environment",
        ]
    )
    _append_json_section(lines, report.env_summary)
    lines.extend(
        [
            "Checks",
        ]
    )
    for check in report.checks:
        lines.append(f"  [{check.status.upper()}] {check.name}: {check.summary}")
        if check.detail:
            lines.append(f"    {check.detail}")
    if report.reconnect_notes:
        lines.append("Reconnect Notes")
        for note in report.reconnect_notes:
            lines.append(f"  - {note}")
    if strict and report.verdict == "warning":
        lines.append("Strict result: blocked")
    sys.stdout.write("\n".join(lines) + "\n")


def _append_json_section(lines: list[str], payload: Any) -> None:
    lines.extend(f"  {line}" for line in json.dumps(payload, indent=2).splitlines())


def _plan_summary(plan: CloudLaunchPlan | object) -> dict[str, Any]:
    if not isinstance(plan, CloudLaunchPlan):
        return {"orchestrator": {}, "workers": [], "evaluators": []}
    return {
        "orchestrator": _placement_summary(plan.orchestrator),
        "workers": [
            _placement_summary(placement) for placement in plan.worker_placements
        ],
        "evaluators": [
            _placement_summary(placement) for placement in plan.evaluator_placements
        ],
    }


def _placement_summary(placement) -> dict[str, Any]:
    return {
        "instance_profile": placement.instance_profile.name,
        "count": getattr(placement, "count", None),
        "region": placement.region,
        "regions": list(placement.regions),
        "zones": list(placement.zones),
        "fallback": placement.fallback,
    }


def _resolved_defaults(
    config: "ExperimentConfig",
    plan: CloudLaunchPlan | object,
) -> dict[str, Any]:
    launch_defaults = None
    if isinstance(plan, CloudLaunchPlan):
        launch_defaults = plan.orchestrator.launch_defaults
    elif config.cloud is not None:
        launch_defaults = config.cloud.defaults
    if launch_defaults is None:
        return {}

    defaults: dict[str, Any] = {}
    for field_name in (
        "readiness_timeout_sec",
        "crsbench_install_spec",
        "crsbench_git_ref",
        "github_deploy_key_path",
    ):
        value = getattr(launch_defaults, field_name, None)
        if value is not None:
            defaults[field_name] = value
    return defaults


def _env_summary(
    config: "ExperimentConfig",
    provider: str,
    plan: CloudLaunchPlan | object,
) -> dict[str, Any]:
    if config.cloud is None:
        return {
            "orchestrator": {"layer_order": [], "layers": []},
            "workers": [],
            "evaluators": [],
        }

    cloud_config = config.cloud
    workers_config = cloud_config.workers
    evaluators_config = cloud_config.evaluators
    orchestrator_config = cloud_config.orchestrator
    assert workers_config is not None
    assert orchestrator_config is not None
    worker_defaults_env = dict(
        getattr(getattr(workers_config, "defaults", None), "env", {})
    )
    worker_placements = list(getattr(workers_config, "placements", []))
    evaluator_defaults_env = (
        dict(getattr(getattr(evaluators_config, "defaults", None), "env", {}))
        if evaluators_config is not None
        else {}
    )
    evaluator_placements = (
        list(getattr(evaluators_config, "placements", []))
        if evaluators_config is not None
        else []
    )

    provider_config = getattr(
        getattr(cloud_config.providers, provider, None), "instance_profiles", {}
    )
    profile_defaults = getattr(
        getattr(
            getattr(cloud_config.providers, provider, None), "profile_defaults", None
        ),
        "env",
        {},
    )
    orchestrator_profile_name = _orchestrator_profile_name(plan, config)
    worker_profile_names = _role_profile_names(plan, config, role="workers")
    evaluator_profile_names = _role_profile_names(plan, config, role="evaluators")

    return {
        "orchestrator": {
            "layer_order": [
                "cloud.env",
                f"cloud.providers.{provider}.profile_defaults.env",
                f"cloud.providers.{provider}.instance_profiles.{orchestrator_profile_name}.env",
                "cloud.orchestrator.env",
                "runtime_managed",
            ],
            "layers": _compact_layers(
                [
                    ("cloud.env", dict(cloud_config.env)),
                    (
                        f"cloud.providers.{provider}.profile_defaults.env",
                        profile_defaults,
                    ),
                    (
                        f"cloud.providers.{provider}.instance_profiles.{orchestrator_profile_name}.env",
                        getattr(
                            provider_config.get(orchestrator_profile_name), "env", {}
                        ),
                    ),
                    (
                        "cloud.orchestrator.env",
                        dict(orchestrator_config.env),
                    ),
                    ("runtime_managed", _RUNTIME_MANAGED_ENV_KEYS),
                ]
            ),
        },
        "workers": [
            {
                "placement_index": index,
                "layer_order": [
                    "cloud.env",
                    f"cloud.providers.{provider}.profile_defaults.env",
                    f"cloud.providers.{provider}.instance_profiles.{profile_name}.env",
                    "cloud.workers.defaults.env",
                    f"cloud.workers.placements[{index}].env",
                    "runtime_managed",
                ],
                "layers": _compact_layers(
                    [
                        ("cloud.env", dict(cloud_config.env)),
                        (
                            f"cloud.providers.{provider}.profile_defaults.env",
                            profile_defaults,
                        ),
                        (
                            f"cloud.providers.{provider}.instance_profiles.{profile_name}.env",
                            getattr(provider_config.get(profile_name), "env", {}),
                        ),
                        (
                            "cloud.workers.defaults.env",
                            worker_defaults_env,
                        ),
                        (
                            f"cloud.workers.placements[{index}].env",
                            dict(worker_placements[index].env)
                            if index < len(worker_placements)
                            else {},
                        ),
                        ("runtime_managed", _RUNTIME_MANAGED_ENV_KEYS),
                    ]
                ),
            }
            for index, profile_name in enumerate(worker_profile_names)
        ],
        "evaluators": [
            {
                "placement_index": index,
                "layer_order": [
                    "cloud.env",
                    f"cloud.providers.{provider}.profile_defaults.env",
                    f"cloud.providers.{provider}.instance_profiles.{profile_name}.env",
                    "cloud.evaluators.defaults.env",
                    f"cloud.evaluators.placements[{index}].env",
                    "runtime_managed",
                ],
                "layers": _compact_layers(
                    [
                        ("cloud.env", dict(cloud_config.env)),
                        (
                            f"cloud.providers.{provider}.profile_defaults.env",
                            profile_defaults,
                        ),
                        (
                            f"cloud.providers.{provider}.instance_profiles.{profile_name}.env",
                            getattr(provider_config.get(profile_name), "env", {}),
                        ),
                        (
                            "cloud.evaluators.defaults.env",
                            evaluator_defaults_env,
                        ),
                        (
                            f"cloud.evaluators.placements[{index}].env",
                            dict(evaluator_placements[index].env)
                            if index < len(evaluator_placements)
                            else {},
                        ),
                        ("runtime_managed", _RUNTIME_MANAGED_ENV_KEYS),
                    ]
                ),
            }
            for index, profile_name in enumerate(evaluator_profile_names)
        ],
    }


def _compact_layers(layer_defs: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for name, values in layer_defs:
        keys = sorted(values) if isinstance(values, dict) else sorted(values)
        if not keys:
            continue
        layers.append(
            {
                "name": name,
                "key_count": len(keys),
                "keys": keys,
            }
        )
    return layers


def _orchestrator_profile_name(
    plan: CloudLaunchPlan | object,
    config: "ExperimentConfig",
) -> str:
    if isinstance(plan, CloudLaunchPlan):
        return plan.orchestrator.instance_profile.name
    if config.cloud is None or config.cloud.orchestrator is None:
        return "unknown"
    return config.cloud.orchestrator.instance_profile


def _role_profile_names(
    plan: CloudLaunchPlan | object,
    config: "ExperimentConfig",
    *,
    role: str,
) -> list[str]:
    if isinstance(plan, CloudLaunchPlan):
        placements = (
            plan.worker_placements if role == "workers" else plan.evaluator_placements
        )
        return [placement.instance_profile.name for placement in placements]
    role_config = getattr(getattr(config, "cloud", None), role, None)
    if role_config is None:
        return []
    return [placement.instance_profile for placement in role_config.placements]


def _remote_root_warning_check(
    config: "ExperimentConfig",
) -> CloudPreflightCheck | None:
    remote = getattr(getattr(config, "cloud", None), "remote", None)
    experiment_root = getattr(remote, "experiment_root", None)
    if experiment_root is not None and str(experiment_root).strip():
        return None
    return CloudPreflightCheck(
        name="remote_experiment_root_fallback",
        status=CloudPreflightCheckStatus.WARNING,
        summary=(
            "cloud.remote.experiment_root is unset; standalone collect and teardown "
            "will fall back to the legacy remote path derived from "
            "storage.experiment_filestore."
        ),
    )
