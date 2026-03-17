"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    load_launch_state,
    save_launch_state,
)
from crsbench.cloud.models import CloudLaunchPlan, build_cloud_launch_plan
from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import create_redis_connection
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import CloudOrchestratorPlacementConfig

if TYPE_CHECKING:
    from crsbench.cloud.readiness import ReadinessRedisProtocol
    from crsbench.distributed.job_lifecycle import LifecycleRedisProtocol
    from crsbench.validation.schemas import GceWorkerFleetConfig


@dataclasses.dataclass(frozen=True)
class ResolvedCloudContext:
    """Resolved cloud runtime context for standalone operational commands."""

    worker_fleet_configs: list["GceWorkerFleetConfig"]
    launch_state: CloudLaunchState | None
    experiment_filestore: Path
    redis_host: str
    redis_password: str | None
    launch_plan: CloudLaunchPlan | None = None

    @property
    def worker_fleet_config(self) -> "GceWorkerFleetConfig | None":
        """Return the lone fleet for legacy callers when only one exists."""
        if len(self.worker_fleet_configs) == 1:
            return self.worker_fleet_configs[0]
        return None


logger = get_logger(__name__)


def resolve_cloud_context(
    config_path: str,
    experiment_name: str,
) -> ResolvedCloudContext:
    """Resolve cloud command context without requiring a live Redis connection."""
    config = load_experiment_config(Path(config_path))
    if config.cloud is None:
        raise SystemExit("Experiment config has no 'cloud' section.")

    launch_plan: CloudLaunchPlan | None = None
    derived_worker_fleets: list["GceWorkerFleetConfig"] = []
    uses_provider_neutral_cloud = (
        config.cloud.providers is not None
        and config.cloud.workers is not None
        and isinstance(config.cloud.orchestrator, CloudOrchestratorPlacementConfig)
    )
    if uses_provider_neutral_cloud:
        launch_plan = build_cloud_launch_plan(config)
        derived_worker_fleets = GceProviderAdapter().build_worker_fleets(launch_plan)
    elif config.cloud.gce is not None:
        derived_worker_fleets = [config.cloud.gce]

    launch_state = load_launch_state(Path(config_path), experiment_name)
    loaded_from_legacy_path = False
    if launch_state is None:
        launch_state = load_launch_state(config.experiment_filestore, experiment_name)
        loaded_from_legacy_path = launch_state is not None

    launch_state_changed = False
    if launch_state is not None:
        launch_state_updates: dict[str, object] = {}
        if launch_state.experiment_filestore is None:
            launch_state_updates["experiment_filestore"] = str(
                config.experiment_filestore
            )
        if not launch_state.worker_fleet_configs and derived_worker_fleets:
            launch_state_updates["worker_fleet_configs"] = derived_worker_fleets
        if launch_state.worker_fleet_config is None and len(derived_worker_fleets) == 1:
            launch_state_updates["worker_fleet_config"] = derived_worker_fleets[0]
        if launch_state_updates:
            launch_state = launch_state.model_copy(update=launch_state_updates)
            launch_state_changed = True
        if loaded_from_legacy_path or launch_state_changed:
            try:
                save_launch_state(Path(config_path), launch_state)
            except OSError as exc:
                logger.warning(
                    "Failed to persist migrated launch state next to config %s: %s",
                    config_path,
                    exc,
                )

    if config.cloud.orchestrator is not None:
        if launch_state is None:
            raise SystemExit(
                "Remote orchestrator launch state not found. "
                "Run `crsbench cloud launch --config ...` first."
            )
        if launch_state.experiment_filestore is None:
            raise SystemExit(
                "Remote orchestrator launch state missing experiment filestore"
            )
        if not launch_state.worker_fleet_configs:
            raise SystemExit(
                "Remote orchestrator launch state missing worker fleet config"
            )
        return ResolvedCloudContext(
            worker_fleet_configs=launch_state.resolved_worker_fleets(),
            launch_state=launch_state,
            experiment_filestore=Path(launch_state.experiment_filestore),
            redis_host=launch_state.redis_host,
            redis_password=launch_state.redis_password,
            launch_plan=launch_plan,
        )

    if not derived_worker_fleets:
        raise SystemExit("Experiment config has no supported cloud worker config.")

    return ResolvedCloudContext(
        worker_fleet_configs=derived_worker_fleets,
        launch_state=launch_state,
        experiment_filestore=Path(config.experiment_filestore),
        redis_host=config.redis_host or "localhost",
        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
        launch_plan=launch_plan,
    )


def reconnect(config_path: str, experiment_name: str):  # noqa: ARG001
    """Bootstrap operational context from a config YAML for standalone cloud commands.

    Args:
        config_path: Path to the experiment YAML config.
        experiment_name: Experiment name (reserved for future use).

    Returns:
        Tuple of (fleet, redis_conn, readiness_store, lifecycle_store, experiment_filestore).

    Raises:
        SystemExit: If the config has no ``cloud`` section.
    """
    context = resolve_cloud_context(config_path, experiment_name)

    if context.redis_password:
        os.environ["CRSBENCH_REDIS_PASSWORD"] = context.redis_password
    else:
        os.environ.pop("CRSBENCH_REDIS_PASSWORD", None)
    redis_conn = create_redis_connection(context.redis_host)

    readiness = CloudReadinessStore(cast("ReadinessRedisProtocol", redis_conn))
    lifecycle = JobLifecycleStore(cast("LifecycleRedisProtocol", redis_conn))

    return context, redis_conn, readiness, lifecycle, context.experiment_filestore
