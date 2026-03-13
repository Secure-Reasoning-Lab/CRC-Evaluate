"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import create_redis_connection
from crsbench.run_experiment import load_experiment_config

if TYPE_CHECKING:
    from crsbench.cloud.readiness import ReadinessRedisProtocol
    from crsbench.distributed.job_lifecycle import LifecycleRedisProtocol


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
    config = load_experiment_config(Path(config_path))
    if config.cloud is None:
        raise SystemExit("Experiment config has no 'cloud' section.")

    fleet = config.cloud.gce
    redis_host = config.redis_host or "localhost"
    redis_conn = create_redis_connection(redis_host)

    readiness = CloudReadinessStore(cast("ReadinessRedisProtocol", redis_conn))
    lifecycle = JobLifecycleStore(cast("LifecycleRedisProtocol", redis_conn))

    return fleet, redis_conn, readiness, lifecycle, config.experiment_filestore
