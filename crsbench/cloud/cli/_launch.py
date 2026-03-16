"""Launch sub-action for local-machine orchestrator + worker provisioning."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError
from crsbench.cloud.launch_state import CloudLaunchState, save_launch_state
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def run_launch(args: argparse.Namespace) -> int:
    """Provision a remote orchestrator VM first, then point workers at its Redis."""
    config_path = Path(args.config)
    config = load_experiment_config(config_path)

    if config.cloud is None or config.cloud.gce is None:
        logger.error("Experiment config must define cloud.gce for cloud launch")
        return 1
    if config.cloud.orchestrator is None:
        logger.error(
            "Experiment config must define cloud.orchestrator for remote launch"
        )
        return 1

    provisioner = GceProvisioner()
    registration = (
        RuntimeRegistration.from_experiment_config(config)
        if isinstance(config, BaseModel)
        else None
    )
    redis_password = secrets.token_urlsafe(24)
    orchestrator_record = None
    workers = []

    try:
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
        workers = provisioner.create_workers(
            experiment_name=config.experiment,
            fleet=config.cloud.gce,
            redis_host=redis_host,
            redis_password=redis_password,
            registration=registration,
        )

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
    except Exception as exc:
        if workers:
            try:
                provisioner.delete_workers(
                    experiment_name=config.experiment,
                    fleet=config.cloud.gce,
                )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for worker fleet in experiment %s",
                    config.experiment,
                )
        if orchestrator_record is not None:
            try:
                provisioner.delete_orchestrators(
                    experiment_name=config.experiment,
                    orchestrator=config.cloud.orchestrator,
                )
            except Exception:
                logger.warning(
                    "Best-effort rollback failed for orchestrator %s",
                    orchestrator_record.name,
                )
        logger.error("Cloud launch failed: %s", exc)
        return 1

    logger.info(
        "Cloud launch complete: orchestrator=%s redis=%s workers=%d",
        orchestrator_record.name,
        f"{orchestrator_record.internal_ip}:6379",
        len(workers),
    )
    return 0
