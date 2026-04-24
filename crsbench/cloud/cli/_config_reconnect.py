"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

import atexit
import dataclasses
import os
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, cast

from crsbench.cloud.launch_state import (
    CloudLaunchState,
    find_launch_state_for_source_experiment,
    load_launch_state,
    save_launch_state,
)
from crsbench.cloud.models import CloudLaunchPlan, build_cloud_launch_plan
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.cloud.providers import provider_adapter_for_launch_plan
from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import (
    create_redis_connection,
    wait_for_redis_connection,
)
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger
from crsbench.utils.storage_warning import warn_for_persisted_storage_roots
from crsbench.validation.schemas import CloudOrchestratorPlacementConfig

if TYPE_CHECKING:
    from crsbench.cloud.readiness import ReadinessRedisProtocol
    from crsbench.cloud.records import CloudFleetPlacementRecord
    from crsbench.distributed.job_lifecycle import LifecycleRedisProtocol


@dataclasses.dataclass(frozen=True)
class ResolvedCloudContext:
    """Resolved cloud runtime context for standalone operational commands."""

    worker_fleet_configs: list[CloudFleetPlacementRecord]
    launch_state: CloudLaunchState | None
    experiment_filestore: Path
    remote_experiment_root: Path
    redis_host: str
    redis_password: str | None
    launch_plan: CloudLaunchPlan | None = None
    evaluator_fleet_configs: list[CloudFleetPlacementRecord] = dataclasses.field(
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
    resolved_config_path = Path(config_path)
    config = load_experiment_config(resolved_config_path)
    launch_state = (
        find_launch_state_for_source_experiment(
            resolved_config_path,
            config.experiment,
        )
        if resolved_config_path.exists()
        else None
    )
    if launch_state is not None:
        return launch_state.effective_remote_experiment_name()
    return config.experiment


def resolve_remote_experiment_dir(
    remote_experiment_root: Path,
    experiment_name: str,
    remote_dir: str | None,
) -> str:
    """Return the remote experiment tree path, inferring from filestore when omitted."""
    if remote_dir:
        return remote_dir
    return str(remote_experiment_root / experiment_name)


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
    derived_worker_fleets: list[CloudFleetPlacementRecord] = []
    derived_evaluator_fleets: list[CloudFleetPlacementRecord] = []
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
    adapter = provider_adapter_for_launch_plan(launch_plan)
    derived_worker_fleets = [
        adapter.to_cloud_fleet_placement_record(fleet, role="worker")
        for fleet in adapter.build_worker_fleets(launch_plan)
    ]
    derived_evaluator_fleets = [
        adapter.to_cloud_fleet_placement_record(fleet, role="evaluator")
        for fleet in adapter.build_evaluator_fleets(launch_plan)
    ]

    launch_state = load_launch_state(Path(config_path), experiment_name)
    if launch_state is None and Path(config_path).exists():
        try:
            launch_state = find_launch_state_for_source_experiment(
                Path(config_path),
                experiment_name,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    launch_state_changed = False
    if launch_state is not None:
        launch_state_updates: dict[str, object] = {}
        if launch_state.experiment_filestore is None:
            launch_state_updates["experiment_filestore"] = str(
                config.experiment_filestore
            )
        if launch_state.remote_experiment_root is None:
            launch_state_updates["remote_experiment_root"] = str(
                _resolve_remote_experiment_root_from_config(config)
            )
        if (
            launch_state.launch_mode != "reeval"
            and not launch_state.worker_fleet_configs
            and derived_worker_fleets
        ):
            launch_state_updates["worker_fleet_configs"] = derived_worker_fleets
        if not launch_state.evaluator_fleet_configs and derived_evaluator_fleets:
            launch_state_updates["evaluator_fleet_configs"] = derived_evaluator_fleets
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
        if launch_state.remote_experiment_root is None:
            raise SystemExit(
                "Remote orchestrator launch state missing remote experiment root"
            )
        if (
            launch_state.launch_mode != "reeval"
            and not launch_state.worker_fleet_configs
        ):
            raise SystemExit(
                "Remote orchestrator launch state missing worker fleet config"
            )
        context = ResolvedCloudContext(
            worker_fleet_configs=launch_state.resolved_worker_fleets(),
            evaluator_fleet_configs=launch_state.resolved_evaluator_fleets(),
            launch_state=launch_state,
            experiment_filestore=Path(launch_state.experiment_filestore),
            remote_experiment_root=Path(launch_state.remote_experiment_root),
            redis_host=launch_state.redis_host,
            redis_password=launch_state.redis_password,
            launch_plan=launch_plan,
        )
        _warn_for_cloud_storage_roots(context)
        return context

    if not derived_worker_fleets:
        raise SystemExit("Experiment config has no supported cloud worker config.")

    context = ResolvedCloudContext(
        worker_fleet_configs=derived_worker_fleets,
        evaluator_fleet_configs=derived_evaluator_fleets,
        launch_state=launch_state,
        experiment_filestore=Path(config.experiment_filestore),
        remote_experiment_root=_resolve_remote_experiment_root_from_config(config),
        redis_host=config.redis_host or "localhost",
        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
        launch_plan=launch_plan,
    )
    _warn_for_cloud_storage_roots(context)
    return context


def _warn_for_cloud_storage_roots(context: ResolvedCloudContext) -> None:
    """Warn when resolved cloud storage roots use Linux /tmp-backed paths."""
    warn_for_persisted_storage_roots(
        experiment_filestore=context.experiment_filestore,
        report_filestore=None,
        copy_results_after_trial=False,
        results_filestore=None,
        remote_experiment_root=context.remote_experiment_root,
    )


def _resolve_remote_experiment_root_from_config(config) -> Path:
    """Return the remote VM experiment root configured for cloud collection."""
    remote = getattr(config.cloud, "remote", None) if config.cloud is not None else None
    experiment_root = getattr(remote, "experiment_root", None)
    if experiment_root is not None:
        return Path(experiment_root)
    return Path(config.experiment_filestore)


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
