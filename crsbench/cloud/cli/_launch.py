"""Launch sub-action for local-machine orchestrator + worker provisioning."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    CreatedCloudInstanceRecord,
    append_created_instance_records,
    save_launch_state,
)
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator
from crsbench.cloud.types import CloudProvider
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import (
    CloudOrchestratorPlacementConfig,
    GceOrchestratorConfig,
    GceWorkerFleetConfig,
)

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def _project_for_worker_record(
    worker_name: str,
    *,
    worker_zone: str,
    worker_fleet_configs: list[GceWorkerFleetConfig],
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
    fleet_configs: list[GceWorkerFleetConfig],
) -> str | None:
    return _project_for_worker_record(
        instance_name,
        worker_zone=instance_zone,
        worker_fleet_configs=fleet_configs,
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
    legacy_orchestrator = None
    legacy_fleet = None
    resolved_legacy_orchestrator = None
    resolved_legacy_fleet = None

    uses_provider_neutral_cloud = (
        config.cloud.providers is not None
        and config.cloud.workers is not None
        and isinstance(config.cloud.orchestrator, CloudOrchestratorPlacementConfig)
    )

    try:
        if uses_provider_neutral_cloud:
            if registration is None:
                raise GceProvisioningError(
                    "Runtime registration is required for provider-neutral cloud launch"
                )

            launch_plan = build_cloud_launch_plan(config)
            preflight = prepare_gce_launch_inputs(
                plan=launch_plan,
                bootstrap=config.cloud.bootstrap,
                cwd=Path.cwd(),
            )
            provisioning_plan = preflight.resolved_plan
            assert provisioning_plan is not None
            assert preflight.redacted_worker_fleets is not None
            adapter = GceProviderAdapter()
            resolved_orchestrator_config = adapter.build_orchestrator_config(
                provisioning_plan
            )
            validator = QuotaValidator(adapters={"gce": adapter})
            validator.validate(launch_plan)

            orchestrator_record = adapter.create_orchestrator(
                plan=provisioning_plan,
                experiment_config_path=str(config_path),
                env_passthrough=preflight.orchestrator_env,
                redis_password=redis_password,
            )
        else:
            if config.cloud.gce is None:
                logger.error("Experiment config must define cloud.gce for cloud launch")
                return 1
            if config.cloud.orchestrator is None:
                logger.error(
                    "Experiment config must define cloud.orchestrator for remote launch"
                )
                return 1

            legacy_fleet = config.cloud.gce
            legacy_orchestrator = cast(
                "GceOrchestratorConfig", config.cloud.orchestrator
            )
            preflight = prepare_gce_launch_inputs(
                orchestrator=legacy_orchestrator,
                worker_fleets=[legacy_fleet],
                bootstrap=config.cloud.bootstrap,
                cwd=Path.cwd(),
            )
            resolved_legacy_orchestrator = preflight.resolved_orchestrator
            assert resolved_legacy_orchestrator is not None
            assert preflight.resolved_worker_fleets is not None
            assert preflight.redacted_worker_fleets is not None
            resolved_legacy_fleet = preflight.resolved_worker_fleets[0]
            provisioner = GceProvisioner()
            orchestrator_record = provisioner.create_orchestrator(
                experiment_name=config.experiment,
                orchestrator=resolved_legacy_orchestrator,
                experiment_config_path=str(config_path),
                env_passthrough=preflight.orchestrator_env,
                redis_password=redis_password,
            )

        if not orchestrator_record.internal_ip:
            raise GceProvisioningError(
                f"Provisioned orchestrator {orchestrator_record.name} has no internal IP"
            )

        if uses_provider_neutral_cloud:
            assert resolved_orchestrator_config is not None
            orchestrator_project = resolved_orchestrator_config.project
        else:
            assert resolved_legacy_orchestrator is not None
            orchestrator_project = resolved_legacy_orchestrator.project

        append_created_instance_records(
            config_path,
            experiment_name=config.experiment,
            records=[
                CreatedCloudInstanceRecord(
                    provider=CloudProvider.GCE,
                    project=orchestrator_project,
                    zone=orchestrator_record.zone,
                    instance_name=orchestrator_record.name,
                )
            ],
        )

        redis_host = f"{orchestrator_record.internal_ip}:6379"
        if uses_provider_neutral_cloud:
            assert adapter is not None
            assert provisioning_plan is not None
            workers = adapter.create_workers(
                plan=provisioning_plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=preflight.worker_env,
            )
            evaluators = adapter.create_evaluators(
                plan=provisioning_plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                experiment_config_path=str(config_path),
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=preflight.evaluator_env,
            )
        else:
            assert resolved_legacy_fleet is not None
            provisioner = GceProvisioner()
            workers = provisioner.create_workers(
                experiment_name=config.experiment,
                fleet=resolved_legacy_fleet,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=preflight.worker_env,
            )

        worker_fleet_configs: list[GceWorkerFleetConfig]
        evaluator_fleet_configs: list[GceWorkerFleetConfig]
        if uses_provider_neutral_cloud:
            assert resolved_orchestrator_config is not None
            assert preflight.redacted_worker_fleets is not None
            worker_fleet_configs = preflight.redacted_worker_fleets
            evaluator_fleet_configs = preflight.redacted_evaluator_fleets or []
            orchestrator_ssh_via_iap = resolved_orchestrator_config.ssh_via_iap
        else:
            assert resolved_legacy_orchestrator is not None
            assert preflight.redacted_worker_fleets is not None
            worker_fleet_configs = preflight.redacted_worker_fleets
            evaluator_fleet_configs = []
            orchestrator_ssh_via_iap = resolved_legacy_orchestrator.ssh_via_iap

        if workers:
            append_created_instance_records(
                config_path,
                experiment_name=config.experiment,
                records=[
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
                ],
            )

        if evaluators:
            append_created_instance_records(
                config_path,
                experiment_name=config.experiment,
                records=[
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
                ],
            )

        save_launch_state(
            config_path,
            CloudLaunchState(
                experiment_name=config.experiment,
                config_path=str(config_path),
                experiment_filestore=str(config.experiment_filestore),
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
                worker_fleet_config=(
                    worker_fleet_configs[0] if len(worker_fleet_configs) == 1 else None
                ),
            ),
        )
    except CloudQuotaValidationError as exc:
        logger.error("Cloud launch failed: {}", str(exc))
        return 1
    except Exception as exc:
        if evaluators:
            try:
                if uses_provider_neutral_cloud:
                    assert adapter is not None
                    assert provisioning_plan is not None
                    adapter.delete_evaluators(plan=provisioning_plan)
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for evaluator fleet in experiment {}",
                    config.experiment,
                )
        if workers:
            try:
                if uses_provider_neutral_cloud:
                    assert adapter is not None
                    assert provisioning_plan is not None
                    adapter.delete_workers(plan=provisioning_plan)
                else:
                    assert resolved_legacy_fleet is not None
                    GceProvisioner().delete_workers(
                        experiment_name=config.experiment,
                        fleet=resolved_legacy_fleet,
                    )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for worker fleet in experiment {}",
                    config.experiment,
                )
        if orchestrator_record is not None:
            try:
                if uses_provider_neutral_cloud:
                    assert adapter is not None
                    assert resolved_orchestrator_config is not None
                    GceProvisioner().delete_instance(
                        project=resolved_orchestrator_config.project,
                        zone=orchestrator_record.zone,
                        instance_name=orchestrator_record.name,
                    )
                else:
                    assert resolved_legacy_orchestrator is not None
                    GceProvisioner().delete_orchestrators(
                        experiment_name=config.experiment,
                        orchestrator=resolved_legacy_orchestrator,
                    )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for orchestrator {}",
                    orchestrator_record.name,
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
