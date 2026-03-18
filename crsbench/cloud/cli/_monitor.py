"""Monitor sub-action for attaching to a launched remote orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import resolve_cloud_context
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.distributed.queue import initialize_queue
from crsbench.distributed.queue_monitor import monitor_queue
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

logger = get_logger(__name__)


def require_launch_state(
    config_path: str,
    experiment_name: str,
) -> "ResolvedCloudContext":
    """Resolve cloud context and require saved remote launch state."""
    context = resolve_cloud_context(config_path, experiment_name)
    if context.launch_state is None:
        raise SystemExit(
            "cloud monitor requires saved remote launch state for the target experiment"
        )
    return context


def run_monitor(args: argparse.Namespace) -> int:
    """Attach to a launched remote orchestrator and show live queue progress."""
    try:
        context = require_launch_state(args.config, args.experiment)
    except SystemExit as exc:
        logger.error(str(exc))
        return 1

    assert context.launch_state is not None
    try:
        with OrchestratorRedisTunnel.from_launch_state(
            Path(args.config),
            context.launch_state,
        ) as tunnel:
            queue = initialize_queue(
                tunnel.redis_host,
                args.experiment,
                redis_password=context.redis_password,
            )
            if queue is None:
                raise RuntimeError(
                    f"Failed to initialize trial queue for experiment {args.experiment}"
                )
            monitor_queue(
                queue,
                args.experiment,
                tracked_job_ids=None,
                tracked_jobs=None,
            )
    except Exception as exc:
        logger.error("Cloud monitor failed: {}", exc)
        return 1
    return 0
