"""Evaluator-local dispatcher runtime helpers."""

from __future__ import annotations

import base64
import socket
import threading
import time
from typing import Any

import rq

from crsbench.distributed.evaluator_dispatcher_state import (
    BuildRequestRecord,
    DispatcherStateStore,
    VerifyRequestRecord,
)
from crsbench.distributed.queue import (
    create_redis_connection,
    resolve_evaluator_local_queue_names,
    validate_queue_name_component,
)
from crsbench.utils.logger import get_logger

EVALUATOR_HEARTBEAT_TTL_SECONDS = 30
DISPATCHER_LEASE_TTL_SECONDS = 15
DISPATCHER_POLL_INTERVAL_SECONDS = 1.0

logger = get_logger(__name__)


def _build_dispatcher_rq_job_id(attempt_id: str) -> str:
    """Encode a logical dispatcher attempt id into an RQ-safe wrapper job id."""
    encoded = base64.urlsafe_b64encode(attempt_id.encode("utf-8")).decode("ascii")
    return f"dispatcher-attempt/{encoded.rstrip('=')}"


def build_evaluator_id(worker_name: str | None) -> str:
    """Derive a stable evaluator identifier from worker identity."""
    raw = (worker_name or socket.gethostname()).strip().lower().replace(".", "-")
    if not raw:
        raw = socket.gethostname().strip().lower().replace(".", "-")
    return validate_queue_name_component(raw)


def heartbeat_evaluator(
    *,
    redis_conn,
    experiment_name: str,
    evaluator_id: str,
    worker_name: str,
) -> None:
    """Refresh evaluator presence in dispatcher state."""
    store = DispatcherStateStore(redis_conn, experiment_name=experiment_name)
    store.upsert_evaluator(
        evaluator_id=evaluator_id,
        worker_name=worker_name,
        expires_in_seconds=EVALUATOR_HEARTBEAT_TTL_SECONDS,
    )


def start_presence_thread(
    *,
    redis_host: str,
    experiment_name: str,
    evaluator_id: str,
    worker_name: str,
) -> tuple[threading.Event, threading.Thread]:
    """Start a background heartbeat loop for one evaluator runtime."""
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        redis_conn = create_redis_connection(redis_host)
        heartbeat_evaluator(
            redis_conn=redis_conn,
            experiment_name=experiment_name,
            evaluator_id=evaluator_id,
            worker_name=worker_name,
        )
        while not stop_event.wait(EVALUATOR_HEARTBEAT_TTL_SECONDS / 3):
            heartbeat_evaluator(
                redis_conn=redis_conn,
                experiment_name=experiment_name,
                evaluator_id=evaluator_id,
                worker_name=worker_name,
            )

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"dispatcher-presence-{evaluator_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _next_owner(owner_order: list[str], *, last_owner: str | None) -> str:
    if not owner_order:
        raise ValueError("owner_order must be non-empty")
    if last_owner not in owner_order:
        return owner_order[0]
    index = owner_order.index(last_owner)
    return owner_order[(index + 1) % len(owner_order)]


class EvaluatorDispatcher:
    """Dispatcher leader that places logical work onto evaluator-local queues."""

    def __init__(
        self,
        *,
        redis_conn: Any,
        experiment_name: str,
        evaluator_id: str,
    ) -> None:
        self.redis = redis_conn
        self.experiment_name = experiment_name
        self.evaluator_id = evaluator_id
        self.store = DispatcherStateStore(redis_conn, experiment_name=experiment_name)
        self.last_build_owner: str | None = None
        self.last_verify_owner: str | None = None
        self.last_work_class: str | None = None

    def try_acquire_leader_lease(self, *, now: float) -> bool:
        return self.store.try_acquire_dispatcher_lease(
            self.evaluator_id,
            now=now,
            ttl_seconds=DISPATCHER_LEASE_TTL_SECONDS,
        )

    def _choose_next_build_request(self) -> BuildRequestRecord | None:
        ready = self.store.list_ready_build_requests()
        if not ready:
            return None
        owner_order = list(dict.fromkeys(request.owner_key for request in ready))
        owner = _next_owner(owner_order, last_owner=self.last_build_owner)
        request = next(request for request in ready if request.owner_key == owner)
        self.last_build_owner = owner
        return request

    def _choose_next_verify_request(self) -> VerifyRequestRecord | None:
        ready = self.store.list_ready_verify_requests()
        if not ready:
            return None
        owner_order = list(dict.fromkeys(request.owner_key for request in ready))
        owner = _next_owner(owner_order, last_owner=self.last_verify_owner)
        request = next(request for request in ready if request.owner_key == owner)
        self.last_verify_owner = owner
        return request

    def _choose_build_evaluator(self, *, now: float) -> str:
        live = self.store.list_live_evaluators(now=now)
        return live[0] if live else self.evaluator_id

    def dispatch_one_build(self, *, now: float) -> BuildRequestRecord | None:
        request = self._choose_next_build_request()
        if request is None:
            return None
        live = set(self.store.list_live_evaluators(now=now))
        evaluator_id = self.store.lineage_owner(request.lineage_id)
        if evaluator_id is None or (live and evaluator_id not in live):
            evaluator_id = self._choose_build_evaluator(now=now)
            self.store.set_lineage_owner(
                lineage_id=request.lineage_id,
                evaluator_id=evaluator_id,
                generation=request.generation,
            )
        attempt_id = f"{request.request_id}:attempt:{request.generation}"
        self.store.assign_build_attempt(
            request_id=request.request_id,
            evaluator_id=evaluator_id,
            attempt_id=attempt_id,
            generation=request.generation,
        )
        self._enqueue_build_attempt(
            request,
            evaluator_id=evaluator_id,
            attempt_id=attempt_id,
        )
        self.last_work_class = "build"
        return request

    def dispatch_one_verify(self, *, now: float) -> VerifyRequestRecord | None:
        request = self._choose_next_verify_request()
        if request is None:
            return None
        live = set(self.store.list_live_evaluators(now=now))
        evaluator_id = self.store.lineage_owner(request.lineage_id)
        if evaluator_id is None or (live and evaluator_id not in live):
            evaluator_id = self._choose_build_evaluator(now=now)
            self.store.set_lineage_owner(
                lineage_id=request.lineage_id,
                evaluator_id=evaluator_id,
                generation=request.generation,
            )
        attempt_id = f"{request.request_id}:attempt:{request.generation}"
        self.store.assign_verify_attempt(
            request_id=request.request_id,
            evaluator_id=evaluator_id,
            attempt_id=attempt_id,
            generation=request.generation,
        )
        self._enqueue_verify_attempt(
            request,
            evaluator_id=evaluator_id,
            attempt_id=attempt_id,
        )
        self.last_work_class = "verify"
        return request

    def dispatch_one(
        self, *, now: float
    ) -> BuildRequestRecord | VerifyRequestRecord | None:
        has_build = bool(self.store.list_ready_build_requests())
        has_verify = bool(self.store.list_ready_verify_requests())
        if has_build and (not has_verify or self.last_work_class == "verify"):
            return self.dispatch_one_build(now=now)
        if has_verify:
            return self.dispatch_one_verify(now=now)
        return None

    def _enqueue_build_attempt(
        self,
        request: BuildRequestRecord,
        *,
        evaluator_id: str,
        attempt_id: str,
    ) -> None:
        build_queue_name, _verify_queue_name = resolve_evaluator_local_queue_names(
            self.experiment_name,
            evaluator_id,
        )
        queue = rq.Queue(build_queue_name, connection=self.redis)
        queue.enqueue(
            "crsbench.distributed.evaluator_dispatcher_jobs.execute_dispatcher_build_attempt",
            {
                "experiment_name": self.experiment_name,
                "request_id": request.request_id,
                "attempt_id": attempt_id,
                "evaluator_id": evaluator_id,
                "generation": request.generation,
                "lineage_id": request.lineage_id,
                "ci_job_payload": dict(request.payload),
            },
            result_ttl=-1,
            job_id=_build_dispatcher_rq_job_id(attempt_id),
        )

    def _enqueue_verify_attempt(
        self,
        request: VerifyRequestRecord,
        *,
        evaluator_id: str,
        attempt_id: str,
    ) -> None:
        _build_queue_name, verify_queue_name = resolve_evaluator_local_queue_names(
            self.experiment_name,
            evaluator_id,
        )
        queue = rq.Queue(verify_queue_name, connection=self.redis)
        queue.enqueue(
            "crsbench.distributed.evaluator_dispatcher_jobs.execute_dispatcher_verify_attempt",
            {
                "experiment_name": self.experiment_name,
                "request_id": request.request_id,
                "attempt_id": attempt_id,
                "evaluator_id": evaluator_id,
                "generation": request.generation,
                "lineage_id": request.lineage_id,
                "verify_payload": dict(request.payload),
            },
            result_ttl=-1,
            job_id=_build_dispatcher_rq_job_id(attempt_id),
        )

    def handle_dead_evaluators(self, *, now: float) -> None:
        for evaluator_id in self.store.list_dead_evaluators(now=now):
            self.store.requeue_inflight_work_from_dead_evaluator(evaluator_id)


def start_dispatcher_thread(
    dispatcher: EvaluatorDispatcher,
) -> tuple[threading.Event, threading.Thread]:
    """Start the leader-election loop for dispatcher placement."""
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(DISPATCHER_POLL_INTERVAL_SECONDS):
            now = time.time()
            try:
                if not dispatcher.try_acquire_leader_lease(now=now):
                    continue
                dispatcher.handle_dead_evaluators(now=now)
                dispatcher.dispatch_one(now=now)
            except Exception:
                logger.exception("Dispatcher loop iteration failed")

    thread = threading.Thread(
        target=_loop,
        name=f"dispatcher-loop-{dispatcher.evaluator_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
