"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

import atexit
import dataclasses
import os
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, cast

from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    load_launch_state,
    redact_worker_fleet_config,
    save_launch_state,
)
from crsbench.cloud.models import CloudLaunchPlan, build_cloud_launch_plan
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import (
    create_redis_connection,
    wait_for_redis_connection,
)
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
    evaluator_fleet_configs: list["GceWorkerFleetConfig"] = dataclasses.field(
        default_factory=list
    )


logger = get_logger(__name__)
_DEFAULT_REMOTE_REDIS_READY_TIMEOUT_SEC = 300


def resolve_effective_experiment_name(
    config_path: str,
    experiment_name: str | None,
) -> str:
    """Return the CLI experiment name, inferring from config when omitted."""
    if experiment_name:
        return experiment_name
    config = load_experiment_config(Path(config_path))
    return config.experiment


def resolve_remote_experiment_dir(
    experiment_filestore: Path,
    experiment_name: str,
    remote_dir: str | None,
) -> str:
    """Return the remote experiment tree path, inferring from filestore when omitted."""
    if remote_dir:
        return remote_dir
    return str(experiment_filestore / experiment_name)


def _resolve_remote_redis_ready_timeout_sec(context: ResolvedCloudContext) -> int:
    """Return how long reconnect callers should wait for remote Redis startup."""
    launch_plan = context.launch_plan
    launch_defaults = getattr(
        getattr(launch_plan, "orchestrator", None), "launch_defaults", None
    )
    timeout = getattr(launch_defaults, "readiness_timeout_sec", None)
    if isinstance(timeout, int) and timeout > 0:
        return timeout
    return _DEFAULT_REMOTE_REDIS_READY_TIMEOUT_SEC


def _register_tunnel_cleanup(redis_conn, tunnel: OrchestratorRedisTunnel) -> None:
    """Keep a remote Redis tunnel alive for the session and stop it on teardown."""
    atexit.register(tunnel.stop)
    try:
        weakref.finalize(redis_conn, tunnel.stop)
    except TypeError:
        pass
    try:
        redis_conn._crsbench_orchestrator_tunnel = tunnel
    except Exception:
        logger.debug("Unable to attach orchestrator tunnel handle to Redis client")


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
    derived_evaluator_fleets: list["GceWorkerFleetConfig"] = []
    uses_provider_neutral_cloud = (
        config.cloud.providers is not None
        and config.cloud.workers is not None
        and isinstance(config.cloud.orchestrator, CloudOrchestratorPlacementConfig)
    )
    if not uses_provider_neutral_cloud:
        raise SystemExit(
            "Experiment config must use provider-neutral cloud.providers/cloud.orchestrator/cloud.workers"
        )
    launch_plan = build_cloud_launch_plan(config)
    adapter = GceProviderAdapter()
    derived_worker_fleets = adapter.build_worker_fleets(launch_plan)
    derived_evaluator_fleets = adapter.build_evaluator_fleets(launch_plan)

    launch_state = load_launch_state(Path(config_path), experiment_name)
    launch_state_changed = False
    if launch_state is not None:
        launch_state_updates: dict[str, object] = {}
        if launch_state.experiment_filestore is None:
            launch_state_updates["experiment_filestore"] = str(
                config.experiment_filestore
            )
        if not launch_state.worker_fleet_configs and derived_worker_fleets:
            launch_state_updates["worker_fleet_configs"] = [
                redact_worker_fleet_config(fleet) for fleet in derived_worker_fleets
            ]
        if not launch_state.evaluator_fleet_configs and derived_evaluator_fleets:
            launch_state_updates["evaluator_fleet_configs"] = [
                redact_worker_fleet_config(fleet) for fleet in derived_evaluator_fleets
            ]
        if launch_state_updates:
            launch_state = launch_state.model_copy(update=launch_state_updates)
            launch_state_changed = True
        if launch_state_changed:
            try:
                save_launch_state(Path(config_path), launch_state)
            except OSError as exc:
                logger.warning(
                    "Failed to persist migrated launch state next to config {}: {}",
                    config_path,
                    exc,
                )

    if launch_state is not None and config.cloud.orchestrator is not None:
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
            evaluator_fleet_configs=launch_state.resolved_evaluator_fleets(),
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
        evaluator_fleet_configs=derived_evaluator_fleets,
        launch_state=launch_state,
        experiment_filestore=Path(config.experiment_filestore),
        redis_host=config.redis_host or "localhost",
        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
        launch_plan=launch_plan,
    )


def reconnect(
    config_path: str,
    experiment_name: str,
    *,
    wait_for_remote_redis: bool = False,
):  # noqa: ARG001
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

    tunnel: OrchestratorRedisTunnel | None = None
    redis_host = context.redis_host
    if context.launch_state is not None:
        tunnel = OrchestratorRedisTunnel.from_launch_state(
            Path(config_path),
            context.launch_state,
        )
        tunnel.start()
        redis_host = tunnel.redis_host
        if wait_for_remote_redis:
            wait_for_redis_connection(
                redis_host,
                redis_password=context.redis_password,
                timeout_sec=_resolve_remote_redis_ready_timeout_sec(context),
            )

    try:
        redis_conn = create_redis_connection(redis_host)
        readiness = CloudReadinessStore(cast("ReadinessRedisProtocol", redis_conn))
        lifecycle = JobLifecycleStore(cast("LifecycleRedisProtocol", redis_conn))
    except Exception:
        if tunnel is not None:
            tunnel.stop()
        raise

    if tunnel is not None:
        _register_tunnel_cleanup(redis_conn, tunnel)

    return context, redis_conn, readiness, lifecycle, context.experiment_filestore
