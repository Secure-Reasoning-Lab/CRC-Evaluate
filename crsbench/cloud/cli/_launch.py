"""Launch sub-action for local-machine orchestrator + worker provisioning."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError
from crsbench.cloud.launch_state import CloudLaunchState, save_launch_state
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator
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
    redis_password = secrets.token_urlsafe(24)
    orchestrator_record = None
    workers = []
    launch_plan = None
    adapter = None
    resolved_orchestrator_config = None
    legacy_orchestrator = None
    legacy_fleet = None

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
            adapter = GceProviderAdapter()
            resolved_orchestrator_config = adapter.build_orchestrator_config(
                launch_plan
            )
            validator = QuotaValidator(adapters={"gce": adapter})
            validator.validate(launch_plan)

            orchestrator_record = adapter.create_orchestrator(
                plan=launch_plan,
                experiment_config_path=str(config_path),
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
            provisioner = GceProvisioner()
            orchestrator_record = provisioner.create_orchestrator(
                experiment_name=config.experiment,
                orchestrator=legacy_orchestrator,
                experiment_config_path=str(config_path),
                redis_password=redis_password,
            )

        if not orchestrator_record.internal_ip:
            raise GceProvisioningError(
                f"Provisioned orchestrator {orchestrator_record.name} has no internal IP"
            )

        redis_host = f"{orchestrator_record.internal_ip}:6379"
        if uses_provider_neutral_cloud:
            assert adapter is not None
            assert launch_plan is not None
            workers = adapter.create_workers(
                plan=launch_plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
            )
        else:
            assert legacy_fleet is not None
            provisioner = GceProvisioner()
            workers = provisioner.create_workers(
                experiment_name=config.experiment,
                fleet=legacy_fleet,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
            )

        worker_fleet_configs: list[GceWorkerFleetConfig]
        if uses_provider_neutral_cloud:
            assert adapter is not None
            assert launch_plan is not None
            assert resolved_orchestrator_config is not None
            worker_fleet_configs = adapter.build_worker_fleets(launch_plan)
            orchestrator_project = resolved_orchestrator_config.project
            orchestrator_ssh_via_iap = resolved_orchestrator_config.ssh_via_iap
        else:
            assert legacy_fleet is not None
            assert legacy_orchestrator is not None
            worker_fleet_configs = [legacy_fleet]
            orchestrator_project = legacy_orchestrator.project
            orchestrator_ssh_via_iap = legacy_orchestrator.ssh_via_iap

        save_launch_state(
            config_path,
            CloudLaunchState(
                experiment_name=config.experiment,
                config_path=str(config_path),
                experiment_filestore=str(config.experiment_filestore),
                redis_host=redis_host,
                redis_password=redis_password,
                orchestrator_provider="gce",
                orchestrator_name=orchestrator_record.name,
                orchestrator_project=orchestrator_project,
                orchestrator_zone=orchestrator_record.zone,
                orchestrator_internal_ip=orchestrator_record.internal_ip,
                orchestrator_external_ip=orchestrator_record.external_ip,
                orchestrator_ssh_via_iap=orchestrator_ssh_via_iap,
                worker_fleet_configs=worker_fleet_configs,
                worker_fleet_config=(
                    worker_fleet_configs[0] if len(worker_fleet_configs) == 1 else None
                ),
            ),
        )
    except CloudQuotaValidationError as exc:
        logger.error("Cloud launch failed: {}", str(exc))
        return 1
    except Exception as exc:
        if workers:
            try:
                if uses_provider_neutral_cloud:
                    assert adapter is not None
                    assert launch_plan is not None
                    adapter.delete_workers(plan=launch_plan)
                else:
                    assert legacy_fleet is not None
                    GceProvisioner().delete_workers(
                        experiment_name=config.experiment,
                        fleet=legacy_fleet,
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
                    assert launch_plan is not None
                    assert resolved_orchestrator_config is not None
                    GceProvisioner().delete_instance(
                        project=resolved_orchestrator_config.project,
                        zone=orchestrator_record.zone,
                        instance_name=orchestrator_record.name,
                    )
                else:
                    assert legacy_orchestrator is not None
                    GceProvisioner().delete_orchestrators(
                        experiment_name=config.experiment,
                        orchestrator=legacy_orchestrator,
                    )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for orchestrator {}",
                    orchestrator_record.name,
                )
        logger.error("Cloud launch failed: {}", str(exc))
        return 1

    logger.info(
        "Cloud launch complete: orchestrator={} redis={} workers={}",
        orchestrator_record.name,
        f"{orchestrator_record.internal_ip}:6379",
        len(workers),
    )
    return 0
