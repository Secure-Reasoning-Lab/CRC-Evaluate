"""Monitor sub-action for attaching to a launched remote orchestrator."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import resolve_cloud_context
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.distributed.queue import (
    RedisConnectionProbe,
    initialize_queue,
    probe_redis_connection,
)
from crsbench.distributed.queue_monitor import monitor_queue
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

logger = get_logger(__name__)

_DEFAULT_MONITOR_REDIS_READY_TIMEOUT_SEC = 300
_MONITOR_REDIS_POLL_INTERVAL_SEC = 5.0
_MONITOR_REDIS_PROBE_TIMEOUT_SEC = 2


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


def _resolve_monitor_redis_ready_timeout_sec(context: "ResolvedCloudContext") -> int:
    """Return how long cloud monitor should wait for remote Redis startup."""
    launch_plan = context.launch_plan
    launch_defaults = getattr(
        getattr(launch_plan, "orchestrator", None), "launch_defaults", None
    )
    timeout = getattr(launch_defaults, "readiness_timeout_sec", None)
    if isinstance(timeout, int) and timeout > 0:
        return timeout
    return _DEFAULT_MONITOR_REDIS_READY_TIMEOUT_SEC


def wait_for_remote_redis(
    redis_host: str,
    *,
    redis_password: str | None,
    timeout_sec: int,
    poll_interval_sec: float = _MONITOR_REDIS_POLL_INTERVAL_SEC,
) -> None:
    """Wait for tunneled remote Redis/Valkey to become reachable."""
    logger.info(
        "Waiting for remote Redis at {} for up to {}s before attaching monitor",
        redis_host,
        timeout_sec,
    )
    deadline = time.monotonic() + float(timeout_sec)
    last_detail = "Redis did not become ready"

    while True:
        probe_state, detail = probe_redis_connection(
            redis_host,
            timeout=_MONITOR_REDIS_PROBE_TIMEOUT_SEC,
            redis_password=redis_password,
        )
        if probe_state is RedisConnectionProbe.READY:
            return
        if probe_state is RedisConnectionProbe.FATAL:
            raise RuntimeError(
                f"Failed to connect to remote Redis at {redis_host}: "
                f"{detail or 'fatal Redis probe error'}"
            )

        last_detail = detail or last_detail
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out waiting for remote Redis at {redis_host} after "
                f"{timeout_sec}s: {last_detail}"
            )
        time.sleep(min(poll_interval_sec, remaining))


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
            wait_for_remote_redis(
                tunnel.redis_host,
                redis_password=context.redis_password,
                timeout_sec=_resolve_monitor_redis_ready_timeout_sec(context),
            )
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
                exit_when_idle=False,
            )
    except Exception as exc:
        logger.error("Cloud monitor failed: {}", exc)
        return 1
    return 0
