"""Evaluator-local dispatcher runtime helpers."""

from __future__ import annotations

import socket
import threading

from crsbench.distributed.evaluator_dispatcher_state import DispatcherStateStore
from crsbench.distributed.queue import (
    create_redis_connection,
    validate_queue_name_component,
)

EVALUATOR_HEARTBEAT_TTL_SECONDS = 30


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
