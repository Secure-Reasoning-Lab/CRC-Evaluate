"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from crsbench.cloud.launch_state import (
    CloudLaunchState,
    load_launch_state,
    save_launch_state,
)
from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import create_redis_connection
from crsbench.run_experiment import load_experiment_config

if TYPE_CHECKING:
    from crsbench.cloud.readiness import ReadinessRedisProtocol
    from crsbench.distributed.job_lifecycle import LifecycleRedisProtocol
    from crsbench.validation.schemas import GceWorkerFleetConfig


def resolve_cloud_context(
    config_path: str,
    experiment_name: str,
) -> tuple["GceWorkerFleetConfig", CloudLaunchState | None, Path, str, str | None]:
    """Resolve cloud command context without requiring a live Redis connection."""
    config = load_experiment_config(Path(config_path))
    if config.cloud is None:
        raise SystemExit("Experiment config has no 'cloud' section.")

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
        if launch_state.worker_fleet_config is None and config.cloud.gce is not None:
            launch_state_updates["worker_fleet_config"] = config.cloud.gce
        if launch_state_updates:
            launch_state = launch_state.model_copy(update=launch_state_updates)
            launch_state_changed = True
        if loaded_from_legacy_path or launch_state_changed:
            save_launch_state(Path(config_path), launch_state)

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
        if launch_state.worker_fleet_config is None:
            raise SystemExit(
                "Remote orchestrator launch state missing worker fleet config"
            )
        return (
            launch_state.worker_fleet_config,
            launch_state,
            Path(launch_state.experiment_filestore),
            launch_state.redis_host,
            launch_state.redis_password,
        )

    if config.cloud.gce is None:
        raise SystemExit("Experiment config has no 'cloud.gce' section.")

    return (
        config.cloud.gce,
        launch_state,
        Path(config.experiment_filestore),
        config.redis_host or "localhost",
        os.environ.get("CRSBENCH_REDIS_PASSWORD"),
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
    (
        fleet,
        _launch_state,
        experiment_filestore,
        redis_host,
        redis_password,
    ) = resolve_cloud_context(config_path, experiment_name)

    if redis_password:
        os.environ["CRSBENCH_REDIS_PASSWORD"] = redis_password
    else:
        os.environ.pop("CRSBENCH_REDIS_PASSWORD", None)
    redis_conn = create_redis_connection(redis_host)

    readiness = CloudReadinessStore(cast("ReadinessRedisProtocol", redis_conn))
    lifecycle = JobLifecycleStore(cast("LifecycleRedisProtocol", redis_conn))

    return fleet, redis_conn, readiness, lifecycle, experiment_filestore
