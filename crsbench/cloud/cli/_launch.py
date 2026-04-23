"""Launch sub-action for local-machine orchestrator + worker provisioning."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.cloud.errors import CloudProvisioningError
from crsbench.cloud.launch_checks import find_launch_target_conflicts
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    CreatedCloudInstanceRecord,
    append_created_instance_records,
    save_launch_state,
)
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.cloud.providers import (
    prepare_launch_inputs,
    provider_adapter_for_launch_plan,
    provisioner_for_provider,
)
from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator
from crsbench.cloud.records import CloudFleetPlacementRecord
from crsbench.cloud.types import CloudProvider
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.experiment.trial_selection import (
    TRIAL_KEY_ALLOWLIST_ENV_VAR,
    derive_unfinished_trial_keys_from_config,
    encode_trial_key_allowlist,
    load_trial_key_file,
)
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.preflight import CloudLaunchPreflight

logger = get_logger(__name__)


def _resolve_trial_selector_env(args: argparse.Namespace, config) -> dict[str, str]:
    if args.rerun_failed_trials and args.only_unfinished_from is None:
        raise CloudProvisioningError(
            "--rerun-failed-trials requires --only-unfinished-from"
        )

    selected_keys: list[str] | None = None
    if args.only_trial_keys_file is not None:
        selected_keys = load_trial_key_file(args.only_trial_keys_file)
    elif args.only_unfinished_from is not None:
        collected_root = Path(args.only_unfinished_from)
        if not collected_root.exists():
            raise CloudProvisioningError(
                f"Collected root does not exist: {collected_root}"
            )
        if not collected_root.is_dir():
            raise CloudProvisioningError(
                f"Collected root is not a directory: {collected_root}"
            )
        selected_keys = derive_unfinished_trial_keys_from_config(
            config,
            collected_root=args.only_unfinished_from,
            rerun_failed_trials=args.rerun_failed_trials,
        ).selected_keys
    if selected_keys is None:
        return {}

    return {
        TRIAL_KEY_ALLOWLIST_ENV_VAR: encode_trial_key_allowlist(selected_keys),
    }


def _normalize_fleet_records(
    adapter,
    fleets: list[CloudFleetPlacementRecord | object],
    *,
    role: str,
) -> list[CloudFleetPlacementRecord]:
    """Normalize launch-state fleet records into provider-neutral placements."""
    normalized: list[CloudFleetPlacementRecord] = []
    for fleet in fleets:
        if isinstance(fleet, CloudFleetPlacementRecord):
            normalized.append(fleet)
            continue
        normalized.append(adapter.to_cloud_fleet_placement_record(fleet, role=role))
    return normalized


def _project_for_worker_record(
    worker_name: str,
    *,
    worker_zone: str,
    worker_fleet_configs: list[CloudFleetPlacementRecord],
) -> str | None:
    zone_projects = {
        fleet.project for fleet in worker_fleet_configs if fleet.zone == worker_zone
    }
    if len(zone_projects) == 1:
        return next(iter(zone_projects))

    all_projects = {fleet.project for fleet in worker_fleet_configs}
    if len(all_projects) == 1:
        return next(iter(all_projects))

    logger.warning(
        "Created instance cache could not determine a unique project for worker {} in zone {}",
        worker_name,
        worker_zone,
    )
    return None


def _project_for_fleet_record(
    instance_name: str,
    *,
    instance_zone: str,
    fleet_configs: list[CloudFleetPlacementRecord],
) -> str | None:
    return _project_for_worker_record(
        instance_name,
        worker_zone=instance_zone,
        worker_fleet_configs=fleet_configs,
    )


def _rollback_created_instances(
    records: list[CreatedCloudInstanceRecord],
) -> None:
    for record in reversed(records):
        if record.project is None:
            logger.warning(
                "Best-effort rollback skipped instance {} because its project is unknown",
                record.instance_name,
            )
            continue
        provisioner = provisioner_for_provider(record.provider)
        provisioner.delete_instance(
            project=record.project,
            zone=record.zone,
            instance_name=record.instance_name,
        )


def run_launch(args: argparse.Namespace) -> int:
    """Provision a remote orchestrator VM first, then point workers at its Redis."""
    config_path = Path(args.config)
    config = load_experiment_config(config_path)

    if config.cloud is None:
        logger.error("Experiment config must define cloud configuration for launch")
        return 1

    registration = (
        RuntimeRegistration.from_experiment_config(config)
        if isinstance(config, BaseModel)
        else None
    )
    bootstrap_inputs = (
        CloudVmBootstrapInputs.from_experiment_config(config)
        if isinstance(config, BaseModel)
        else None
    )
    redis_password = secrets.token_urlsafe(24)
    orchestrator_record = None
    workers = []
    evaluators = []
    launch_plan = None
    provisioning_plan = None
    adapter = None
    resolved_orchestrator_config = None
    worker_fleet_configs: list[CloudFleetPlacementRecord] = []
    evaluator_fleet_configs: list[CloudFleetPlacementRecord] = []
    orchestrator_created_records: list[CreatedCloudInstanceRecord] = []
    worker_created_records: list[CreatedCloudInstanceRecord] = []
    evaluator_created_records: list[CreatedCloudInstanceRecord] = []
    selector_env: dict[str, str] = {}
    try:
        selector_env = _resolve_trial_selector_env(args, config)
        if registration is None:
            raise CloudProvisioningError(
                "Runtime registration is required for cloud launch"
            )

        launch_plan = build_cloud_launch_plan(config)
        preflight: CloudLaunchPreflight = prepare_launch_inputs(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        provisioning_plan = preflight.resolved_plan
        assert preflight.redacted_worker_fleets is not None
        adapter = provider_adapter_for_launch_plan(provisioning_plan)
        resolved_orchestrator_config = adapter.build_orchestrator_config(
            provisioning_plan
        )
        worker_fleet_configs = _normalize_fleet_records(
            adapter,
            list(preflight.redacted_worker_fleets),
            role="worker",
        )
        evaluator_fleet_configs = _normalize_fleet_records(
            adapter,
            list(preflight.redacted_evaluator_fleets or []),
            role="evaluator",
        )
        conflicts = find_launch_target_conflicts(
            config_path=config_path,
            experiment_name=config.experiment,
            adapter=adapter,
            plan=provisioning_plan,
        )
        if conflicts:
            raise CloudProvisioningError(
                f"Experiment {config.experiment!r} already has cloud launch state; "
                f"{'; '.join(conflicts)}. Tear it down before launching again."
            )
        validator = QuotaValidator(adapters={"gce": adapter})
        validator.validate(launch_plan)
        orchestrator_env = dict(preflight.orchestrator_env or {})
        orchestrator_env.update(selector_env)

        orchestrator_record = adapter.create_orchestrator(
            plan=provisioning_plan,
            experiment_config_path=str(config_path),
            env_passthrough=orchestrator_env,
            redis_password=redis_password,
        )

        if not orchestrator_record.internal_ip:
            raise CloudProvisioningError(
                f"Provisioned orchestrator {orchestrator_record.name} has no internal IP"
            )

        assert resolved_orchestrator_config is not None
        orchestrator_project = resolved_orchestrator_config.project

        append_created_instance_records(
            config_path,
            experiment_name=config.experiment,
            records=(
                orchestrator_created_records := [
                    CreatedCloudInstanceRecord(
                        provider=CloudProvider.GCE,
                        project=orchestrator_project,
                        zone=orchestrator_record.zone,
                        instance_name=orchestrator_record.name,
                    )
                ]
            ),
        )

        redis_host = f"{orchestrator_record.internal_ip}:6379"
        assert adapter is not None
        workers = adapter.create_workers(
            plan=provisioning_plan,
            redis_host=redis_host,
            redis_password=redis_password,
            registration=registration,
            bootstrap_inputs=bootstrap_inputs,
            env_passthrough_by_placement=preflight.worker_placement_envs,
        )

        if workers:
            worker_created_records = [
                CreatedCloudInstanceRecord(
                    provider=CloudProvider.GCE,
                    project=_project_for_worker_record(
                        worker.name,
                        worker_zone=worker.zone,
                        worker_fleet_configs=worker_fleet_configs,
                    ),
                    zone=worker.zone,
                    instance_name=worker.name,
                )
                for worker in workers
            ]
            append_created_instance_records(
                config_path,
                experiment_name=config.experiment,
                records=worker_created_records,
            )

        evaluators = adapter.create_evaluators(
            plan=provisioning_plan,
            redis_host=redis_host,
            redis_password=redis_password,
            registration=registration,
            experiment_config_path=str(config_path),
            bootstrap_inputs=bootstrap_inputs,
            env_passthrough_by_placement=preflight.evaluator_placement_envs,
        )
        orchestrator_ssh_via_iap = resolved_orchestrator_config.ssh_via_iap

        if evaluators:
            evaluator_created_records = [
                CreatedCloudInstanceRecord(
                    provider=CloudProvider.GCE,
                    project=_project_for_fleet_record(
                        worker.name,
                        instance_zone=worker.zone,
                        fleet_configs=evaluator_fleet_configs,
                    ),
                    zone=worker.zone,
                    instance_name=worker.name,
                )
                for worker in evaluators
            ]
            append_created_instance_records(
                config_path,
                experiment_name=config.experiment,
                records=evaluator_created_records,
            )

        save_launch_state(
            config_path,
            CloudLaunchState(
                experiment_name=config.experiment,
                config_path=str(config_path),
                experiment_filestore=str(config.experiment_filestore),
                remote_experiment_root=str(
                    config.cloud.remote.experiment_root
                    if config.cloud is not None
                    and config.cloud.remote.experiment_root is not None
                    else config.experiment_filestore
                ),
                redis_host=redis_host,
                redis_password=redis_password,
                orchestrator_provider=CloudProvider.GCE,
                orchestrator_name=orchestrator_record.name,
                orchestrator_project=orchestrator_project,
                orchestrator_zone=orchestrator_record.zone,
                orchestrator_internal_ip=orchestrator_record.internal_ip,
                orchestrator_external_ip=orchestrator_record.external_ip,
                orchestrator_ssh_via_iap=orchestrator_ssh_via_iap,
                worker_fleet_configs=worker_fleet_configs,
                evaluator_fleet_configs=evaluator_fleet_configs,
            ),
        )
    except CloudQuotaValidationError as exc:
        logger.error("Cloud launch failed: {}", str(exc))
        return 1
    except Exception as exc:
        if evaluator_created_records:
            try:
                _rollback_created_instances(evaluator_created_records)
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for created evaluator instances in experiment {}",
                    config.experiment,
                )
        if worker_created_records:
            try:
                _rollback_created_instances(worker_created_records)
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for created worker instances in experiment {}",
                    config.experiment,
                )
        if orchestrator_created_records:
            try:
                _rollback_created_instances(orchestrator_created_records)
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for orchestrator {}",
                    orchestrator_record.name
                    if orchestrator_record is not None
                    else "?",
                )
        logger.error("Cloud launch failed: {}", str(exc))
        return 1

    logger.info(
        "Cloud launch complete: orchestrator={} redis={} workers={} evaluators={}",
        orchestrator_record.name,
        f"{orchestrator_record.internal_ip}:6379",
        len(workers),
        len(evaluators),
    )
    return 0
