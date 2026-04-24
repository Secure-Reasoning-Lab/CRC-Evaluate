"""Dispatcher-local warmup feeder for evaluator build queues."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import rq

from crsbench.distributed.evaluator_dispatcher_state import (
    DispatcherStateRedisProtocol,
    DispatcherStateStore,
)
from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    build_scheduler_owner_key_for_ci_job,
)
from crsbench.distributed.queue import create_redis_connection
from crsbench.utils.benchmark_utils import filter_benchmarks_by_mode
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

logger = get_logger(__name__)

DISPATCHER_WARMUP_POLL_INTERVAL_SECONDS = 1.0


class _ModeProtocol(Protocol):
    value: str


class _ResourcesProtocol(Protocol):
    cpu_tag: str | None


class WarmupConfigProtocol(Protocol):
    benchmarks_root: Path | str
    mode: _ModeProtocol
    resources: _ResourcesProtocol | None

    def get_benchmark_list(self) -> list[str]: ...


@dataclass(frozen=True)
class WarmupBuildSpec:
    job_id: str
    payload: dict[str, Any]
    meta: dict[str, Any]


def _is_duplicate_job_enqueue_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "already exists" in msg or "job id" in msg and "exists" in msg


class DispatcherWarmupFeeder:
    """Feed optional warmup build jobs into one evaluator-local build queue."""

    def __init__(
        self,
        *,
        build_queue: "rq.Queue",
        state_store: DispatcherStateStore,
        warmup_specs: list[WarmupBuildSpec],
        build_capacity: int,
    ) -> None:
        self.build_queue = build_queue
        self.state_store = state_store
        self.warmup_specs = warmup_specs
        self.build_capacity = max(1, int(build_capacity))
        self._next_spec_index = 0

    def _current_backlog(self) -> int:
        queued = int(self.build_queue.count)
        intermediate = len(list(self.build_queue.intermediate_queue.get_job_ids()))
        started = int(self.build_queue.started_job_registry.count)
        return queued + intermediate + started

    def tick(self) -> int:
        """Queue at most spare-capacity warmup jobs when no required demand exists."""
        if self.state_store.has_pending_required_builds():
            return 0

        spare_capacity = self.build_capacity - self._current_backlog()
        if spare_capacity <= 0:
            return 0

        enqueued = 0
        while enqueued < spare_capacity and self._next_spec_index < len(
            self.warmup_specs
        ):
            spec = self.warmup_specs[self._next_spec_index]
            self._next_spec_index += 1
            try:
                self.build_queue.enqueue(
                    "crsbench.distributed.build_jobs.execute_ci_build",
                    spec.payload,
                    job_timeout=3600,
                    result_ttl=-1,
                    job_id=spec.job_id,
                    meta=spec.meta,
                )
                enqueued += 1
            except Exception as exc:
                if _is_duplicate_job_enqueue_error(exc):
                    continue
                logger.exception(
                    "Failed to enqueue dispatcher warmup job {}", spec.job_id
                )
                raise
        return enqueued


def build_dispatcher_warmup_specs(
    config: WarmupConfigProtocol,
    *,
    experiment_name: str,
    inc_image_policy: str,
    inc_image_registry: str,
    inc_image_max_pull_bytes: int | None,
    inc_image_pull_timeout: int,
    local_image_prefix: str,
) -> list[WarmupBuildSpec]:
    """Build optional warmup build specs for all experiment benchmarks."""
    from crsbench.distributed.ci_jobs import serialize_ci_job
    from crsbench.executor.variant_planner import VariantPlanner

    benchmarks_root = Path(config.benchmarks_root)
    benchmark_names = list(config.get_benchmark_list())

    mode_str = config.mode.value
    if mode_str not in ("all", "auto"):
        benchmark_names = filter_benchmarks_by_mode(
            benchmark_names, mode_str, benchmarks_root
        )

    planner = VariantPlanner(
        Path(ensure_oss_fuzz_root()),
        source_mode=getattr(config, "source_mode", "pkgs"),
    )

    cpu_tag = getattr(getattr(config, "resources", None), "cpu_tag", None)
    base_meta: dict[str, Any] = {"experiment_name": experiment_name}
    if cpu_tag:
        base_meta["cpu_tag"] = cpu_tag

    specs: list[WarmupBuildSpec] = []
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Warmup skip: {benchmark_path} not found")
            continue
        jobs = planner.plan_builds(
            benchmark_path,
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy=inc_image_policy,
            inc_image_registry=inc_image_registry,
            inc_image_max_pull_bytes=inc_image_max_pull_bytes,
            inc_image_pull_timeout=inc_image_pull_timeout,
            local_image_prefix=local_image_prefix,
        )
        for job in jobs:
            meta = dict(base_meta)
            meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_for_ci_job(
                job,
                experiment_name=experiment_name,
            )
            specs.append(
                WarmupBuildSpec(
                    job_id=job.job_id,
                    payload=serialize_ci_job(job),
                    meta=meta,
                )
            )
    return specs


def start_dispatcher_warmup_thread(
    *,
    redis_host: str,
    config: WarmupConfigProtocol,
    experiment_name: str,
    build_queue_name: str,
    build_capacity: int,
    inc_image_policy: str,
    inc_image_registry: str,
    inc_image_max_pull_bytes: int | None,
    inc_image_pull_timeout: int,
    local_image_prefix: str,
    poll_interval_seconds: float = DISPATCHER_WARMUP_POLL_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    """Start a daemon loop that opportunistically feeds warmup builds."""
    warmup_specs = build_dispatcher_warmup_specs(
        config,
        experiment_name=experiment_name,
        inc_image_policy=inc_image_policy,
        inc_image_registry=inc_image_registry,
        inc_image_max_pull_bytes=inc_image_max_pull_bytes,
        inc_image_pull_timeout=inc_image_pull_timeout,
        local_image_prefix=local_image_prefix,
    )
    redis_conn = create_redis_connection(redis_host)
    state_store = DispatcherStateStore(
        cast("DispatcherStateRedisProtocol", redis_conn),
        experiment_name=experiment_name,
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=rq.Queue(build_queue_name, connection=redis_conn),
        state_store=state_store,
        warmup_specs=warmup_specs,
        build_capacity=build_capacity,
    )

    stop_event = threading.Event()

    def _loop() -> None:
        while True:
            if stop_event.is_set():
                return
            try:
                feeder.tick()
            except Exception:
                logger.exception("Dispatcher warmup feeder loop iteration failed")
            if stop_event.wait(poll_interval_seconds):
                return

    thread = threading.Thread(
        target=_loop,
        name=f"dispatcher-warmup-{experiment_name}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
