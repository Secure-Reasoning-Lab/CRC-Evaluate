"""Launch sub-action for local-machine orchestrator + worker provisioning."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError
from crsbench.cloud.launch_state import CloudLaunchState, save_launch_state
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import CloudOrchestratorPlacementConfig

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

            provisioner = GceProvisioner()
            orchestrator_record = provisioner.create_orchestrator(
                experiment_name=config.experiment,
                orchestrator=config.cloud.orchestrator,
                experiment_config_path=str(config_path),
                redis_password=redis_password,
            )

        if not orchestrator_record.internal_ip:
            raise GceProvisioningError(
                f"Provisioned orchestrator {orchestrator_record.name} has no internal IP"
            )

        redis_host = f"{orchestrator_record.internal_ip}:6379"
        if uses_provider_neutral_cloud:
            launch_plan = build_cloud_launch_plan(config)
            adapter = GceProviderAdapter()
            workers = adapter.create_workers(
                plan=launch_plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
            )
        else:
            provisioner = GceProvisioner()
            workers = provisioner.create_workers(
                experiment_name=config.experiment,
                fleet=config.cloud.gce,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
            )

        if not uses_provider_neutral_cloud:
            save_launch_state(
                config_path,
                CloudLaunchState(
                    experiment_name=config.experiment,
                    config_path=str(config_path),
                    experiment_filestore=str(config.experiment_filestore),
                    redis_host=redis_host,
                    redis_password=redis_password,
                    orchestrator_name=orchestrator_record.name,
                    orchestrator_project=config.cloud.orchestrator.project,
                    orchestrator_zone=orchestrator_record.zone,
                    orchestrator_internal_ip=orchestrator_record.internal_ip,
                    orchestrator_external_ip=orchestrator_record.external_ip,
                    orchestrator_ssh_via_iap=config.cloud.orchestrator.ssh_via_iap,
                    worker_fleet_config=config.cloud.gce,
                ),
            )
    except CloudQuotaValidationError as exc:
        logger.error("Cloud launch failed: {}", str(exc))
        return 1
    except Exception as exc:
        if workers:
            try:
                if uses_provider_neutral_cloud:
                    GceProviderAdapter().delete_workers(
                        plan=build_cloud_launch_plan(config),
                    )
                else:
                    GceProvisioner().delete_workers(
                        experiment_name=config.experiment,
                        fleet=config.cloud.gce,
                    )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for worker fleet in experiment {}",
                    config.experiment,
                )
        if orchestrator_record is not None:
            try:
                if uses_provider_neutral_cloud:
                    GceProvisioner().delete_instance(
                        project=build_cloud_launch_plan(
                            config
                        ).orchestrator.instance_profile.provider_config["project"],
                        zone=orchestrator_record.zone,
                        instance_name=orchestrator_record.name,
                    )
                else:
                    GceProvisioner().delete_orchestrators(
                        experiment_name=config.experiment,
                        orchestrator=config.cloud.orchestrator,
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
