"""Dispatcher-local warmup feeder for evaluator build queues."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from crsbench.distributed.evaluator_claim_worker import build_local_ci_job_id
from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    build_scheduler_owner_key_for_ci_job,
)
from crsbench.distributed.queue import create_redis_connection
from crsbench.utils.benchmark_utils import filter_benchmarks_by_mode
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

DISPATCHER_WARMUP_POLL_INTERVAL_SECONDS = 1.0
WARMUP_BUILD_JOB_TIMEOUT_SECONDS = 3600

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class _ModeProtocol(Protocol):
    value: str


class WarmupConfigProtocol(Protocol):
    benchmarks_root: Path | str
    mode: _ModeProtocol

    def get_benchmark_list(self) -> list[str]: ...


class _JobIdRegistryProtocol(Protocol):
    def get_job_ids(self) -> list[str]: ...


class BuildQueueProtocol(Protocol):
    intermediate_queue: _JobIdRegistryProtocol
    started_job_registry: _JobIdRegistryProtocol

    def get_job_ids(self) -> list[str]: ...

    def enqueue(
        self,
        func_name: str,
        payload: dict[str, Any],
        *,
        job_timeout: int,
        result_ttl: int,
        job_id: str,
        meta: dict[str, Any],
    ) -> object: ...


class RequiredBuildTrackerProtocol(Protocol):
    def has_pending_required_builds(self) -> bool: ...


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
        build_queue: BuildQueueProtocol,
        required_build_tracker: RequiredBuildTrackerProtocol | None = None,
        state_store: RequiredBuildTrackerProtocol | None = None,
        warmup_specs: Iterable[WarmupBuildSpec],
        build_capacity: int,
    ) -> None:
        self.build_queue = build_queue
        tracker = required_build_tracker or state_store
        if tracker is None:
            raise ValueError(
                "required_build_tracker or state_store is required for warmup feeder"
            )
        self.required_build_tracker = tracker
        self._warmup_specs = iter(warmup_specs)
        self._pending_spec: WarmupBuildSpec | None = None
        self.build_capacity = max(1, int(build_capacity))

    def _current_backlog(self) -> int:
        queued = len(self.build_queue.get_job_ids())
        intermediate = len(self.build_queue.intermediate_queue.get_job_ids())
        started = len(self.build_queue.started_job_registry.get_job_ids())
        return queued + intermediate + started

    def tick(self) -> int:
        """Queue at most spare-capacity warmup jobs when no required demand exists."""
        spare_capacity = self.build_capacity - self._current_backlog()
        if spare_capacity <= 0:
            return 0

        enqueued = 0
        while enqueued < spare_capacity:
            if self.required_build_tracker.has_pending_required_builds():
                break
            spec = self._pending_spec
            if spec is None:
                spec = next(self._warmup_specs, None)
            if spec is None:
                break
            if self.required_build_tracker.has_pending_required_builds():
                self._pending_spec = spec
                break
            self._pending_spec = None
            try:
                self.build_queue.enqueue(
                    "crsbench.distributed.build_jobs.execute_ci_build",
                    spec.payload,
                    job_timeout=WARMUP_BUILD_JOB_TIMEOUT_SECONDS,
                    result_ttl=-1,
                    job_id=spec.job_id,
                    meta=dict(spec.meta),
                )
                enqueued += 1
            except Exception as exc:
                if _is_duplicate_job_enqueue_error(exc):
                    continue
                logger.exception(
                    "Failed to enqueue dispatcher warmup job {}",
                    spec.job_id,
                )
                raise
        return enqueued


def build_dispatcher_warmup_specs(
    config: WarmupConfigProtocol,
    *,
    experiment_name: str,
    evaluator_id: str,
    oss_fuzz_path: Path,
    inc_image_policy: str,
    inc_image_registry: str,
    inc_image_max_pull_bytes: int | None,
    inc_image_pull_timeout: int,
    local_image_prefix: str,
) -> "Iterator[WarmupBuildSpec]":
    """Lazily yield warmup build specs as spare local capacity becomes available."""
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
        oss_fuzz_path,
        source_mode=getattr(config, "source_mode", "pkgs"),
    )

    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Warmup skip: {benchmark_path} not found")
            continue
        jobs = planner.iter_builds(
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
            localized_job = copy.copy(job)
            prepare_inc_job_id = getattr(localized_job, "prepare_inc_job_id", "")
            if isinstance(prepare_inc_job_id, str) and prepare_inc_job_id:
                localized_job.prepare_inc_job_id = build_local_ci_job_id(
                    prepare_inc_job_id,
                    evaluator_id=evaluator_id,
                )
            yield WarmupBuildSpec(
                job_id=build_local_ci_job_id(job.job_id, evaluator_id=evaluator_id),
                payload=serialize_ci_job(localized_job),
                meta={
                    "experiment_name": experiment_name,
                    "warmup": "true",
                    SCHEDULER_OWNER_KEY_META: build_scheduler_owner_key_for_ci_job(
                        job,
                        experiment_name=experiment_name,
                    ),
                },
            )


def start_dispatcher_warmup_thread(
    *,
    redis_host: str,
    config: WarmupConfigProtocol,
    experiment_name: str,
    evaluator_id: str,
    build_queue_name: str,
    build_jobs: int,
    required_build_tracker: RequiredBuildTrackerProtocol,
    oss_fuzz_path: Path,
    inc_image_policy: str,
    inc_image_registry: str,
    inc_image_max_pull_bytes: int | None,
    inc_image_pull_timeout: int,
    local_image_prefix: str,
    poll_interval_seconds: float = DISPATCHER_WARMUP_POLL_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    """Start a daemon loop that opportunistically feeds warmup builds."""
    import rq

    warmup_specs = build_dispatcher_warmup_specs(
        config,
        experiment_name=experiment_name,
        evaluator_id=evaluator_id,
        oss_fuzz_path=oss_fuzz_path,
        inc_image_policy=inc_image_policy,
        inc_image_registry=inc_image_registry,
        inc_image_max_pull_bytes=inc_image_max_pull_bytes,
        inc_image_pull_timeout=inc_image_pull_timeout,
        local_image_prefix=local_image_prefix,
    )
    logger.info("Dispatcher warmup: initialized local build warmup planner")

    redis_conn = create_redis_connection(redis_host)
    feeder = DispatcherWarmupFeeder(
        build_queue=cast(
            "BuildQueueProtocol",
            rq.Queue(build_queue_name, connection=redis_conn),
        ),
        required_build_tracker=required_build_tracker,
        warmup_specs=warmup_specs,
        build_capacity=build_jobs,
    )

    stop_event = threading.Event()

    def _loop() -> None:
        while True:
            try:
                feeder.tick()
            except Exception:
                logger.exception("Dispatcher warmup feeder loop iteration failed")
            if stop_event.wait(poll_interval_seconds):
                return

    thread = threading.Thread(
        target=_loop,
        name=f"dispatcher-warmup-{evaluator_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
