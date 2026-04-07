"""Monitor sub-action for attaching to a launched remote orchestrator."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import rq.job

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.distributed.queue import (
    RedisConnectionProbe,
    initialize_queue,
    is_job_for_experiment,
    probe_redis_connection,
)
from crsbench.distributed.queue_monitor import QueueMonitorCallbacks, monitor_queue
from crsbench.utils.apprise_notify import (
    AppriseNotificationConfig,
    load_apprise_notification_config,
    send_apprise_message,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

logger = get_logger(__name__)

_DEFAULT_MONITOR_REDIS_READY_TIMEOUT_SEC = 300
_MONITOR_REDIS_POLL_INTERVAL_SEC = 5.0
_MONITOR_REDIS_PROBE_TIMEOUT_SEC = 2


@dataclass
class _CloudMonitorNotificationState:
    """Track one cloud monitor session's notification state."""

    queue: object
    experiment_name: str
    notification_config: AppriseNotificationConfig | None
    completion_sent: bool = False
    failure_sent: bool = False
    last_snapshot_active: bool | None = None

    def on_snapshot(self, snapshot) -> None:
        """Send one completion notification on the first active-to-idle transition."""
        if self.completion_sent:
            return

        is_active = self._snapshot_has_pending_work(snapshot)
        if is_active is None:
            return

        if self.last_snapshot_active is None:
            self.last_snapshot_active = is_active
            return

        if self.last_snapshot_active and not is_active:
            self._send_completion_notification(snapshot)
            self.completion_sent = True

        self.last_snapshot_active = is_active

    def _snapshot_has_pending_work(self, snapshot) -> bool | None:
        stats = getattr(snapshot, "stats", {}) or {}
        queued = int(stats.get("queued", 0) or 0)
        started = int(stats.get("started", 0) or 0)
        if queued + started > 0:
            return True
        return self._pending_deferred_or_scheduled_work()

    def _pending_deferred_or_scheduled_work(self) -> bool | None:
        try:
            for registry_name in ("deferred_job_registry", "scheduled_job_registry"):
                registry = getattr(self.queue, registry_name, None)
                get_job_ids = getattr(registry, "get_job_ids", None)
                if not callable(get_job_ids):
                    continue
                for job_id in get_job_ids():
                    job = rq.job.Job.fetch(job_id, connection=self.queue.connection)
                    if job is not None and is_job_for_experiment(
                        job, self.experiment_name
                    ):
                        return True
        except Exception as exc:
            logger.warning(
                "Cloud monitor could not confirm pending deferred/scheduled work: {}",
                exc,
            )
            return None
        return False

    def _send_completion_notification(self, snapshot) -> None:
        if self.notification_config is None:
            return
        stats = getattr(snapshot, "stats", {}) or {}
        queued = int(stats.get("queued", 0) or 0)
        started = int(stats.get("started", 0) or 0)
        finished = int(stats.get("finished", 0) or 0)
        failed = int(stats.get("failed", 0) or 0)
        body = "\n".join(
            [
                "cloud monitor queue drained"
                + (" with failures" if failed > 0 else ""),
                f"Experiment: {self.experiment_name}",
                "Source: cloud monitor",
                f"Queued: {queued}",
                f"Started: {started}",
                f"Finished: {finished}",
                f"Failed: {failed}",
            ]
        )
        send_apprise_message(self.notification_config, body=body)

    def send_failure_notification(self, exc: Exception) -> None:
        if self.notification_config is None or self.failure_sent:
            return
        body = "\n".join(
            [
                f"cloud monitor failed: {exc}",
                f"Experiment: {self.experiment_name}",
                "Source: cloud monitor",
            ]
        )
        self.failure_sent = True
        send_apprise_message(self.notification_config, body=body)


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
        experiment_name = resolve_effective_experiment_name(
            args.config, args.experiment
        )
        context = require_launch_state(args.config, experiment_name)
        assert context.launch_state is not None
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
                experiment_name,
                redis_password=context.redis_password,
            )
            if queue is None:
                raise RuntimeError(
                    f"Failed to initialize trial queue for experiment {experiment_name}"
                )
            notification_state = _CloudMonitorNotificationState(
                queue=queue,
                experiment_name=experiment_name,
                notification_config=load_apprise_notification_config(),
            )
            monitor_started = False
            try:
                monitor_started = True
                monitor_queue(
                    queue,
                    experiment_name,
                    tracked_job_ids=None,
                    tracked_jobs=None,
                    callbacks=QueueMonitorCallbacks(
                        on_snapshot=notification_state.on_snapshot,
                    ),
                    exit_when_idle=False,
                )
            except KeyboardInterrupt:
                return 130
            except Exception as exc:
                if monitor_started:
                    notification_state.send_failure_notification(exc)
                raise
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:
        logger.error("Cloud monitor failed: {}", exc)
        return 1
    return 0
