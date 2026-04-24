"""Dispatcher wrapper jobs for evaluator-local build and verify execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from crsbench.distributed.build_jobs import execute_ci_build
from crsbench.distributed.evaluator_dispatcher_state import (
    BuildResultRecord,
    DispatcherStateStore,
    VerifyResultRecord,
)
from crsbench.distributed.evaluator_jobs import verify_single_pov
from crsbench.distributed.patch_evaluator_jobs import (
    execute_patch_build,
    execute_patch_verify,
)


@dataclass(frozen=True)
class DispatcherAttemptPayload:
    experiment_name: str
    request_id: str
    attempt_id: str
    evaluator_id: str
    generation: int
    lineage_id: str
    verify_payload: dict[str, Any] | None = None
    ci_job_payload: dict[str, Any] | None = None


def _get_dispatcher_store(
    experiment_name: str, *, redis_conn: Any = None
) -> DispatcherStateStore:
    if redis_conn is None:
        try:
            import rq

            current_job = rq.get_current_job()
            if current_job is not None and current_job.connection is not None:
                redis_conn = current_job.connection
        except ImportError:
            pass

    if redis_conn is None:
        from crsbench.distributed.queue import create_redis_connection

        redis_host = os.environ.get("CRSBENCH_REDIS_HOST")
        if not redis_host:
            raise ValueError("CRSBENCH_REDIS_HOST is required for dispatcher jobs")
        redis_conn = create_redis_connection(redis_host)
    return DispatcherStateStore(
        cast("Any", redis_conn), experiment_name=experiment_name
    )


def _publish_build_attempt_result(
    *,
    store: DispatcherStateStore,
    request_id: str,
    attempt_id: str,
    generation: int,
    evaluator_id: str,
    lineage_id: str,
    result_dict: dict[str, Any],
) -> bool:
    terminal_state = "succeeded" if result_dict.get("success") else "failed"
    if not store.publish_build_result_if_current(
        request_id=request_id,
        attempt_id=attempt_id,
        result=BuildResultRecord(
            request_id=request_id,
            attempt_id=attempt_id,
            generation=generation,
            evaluator_id=evaluator_id,
            terminal_state=terminal_state,
        ),
    ):
        return False
    if terminal_state == "succeeded":
        store.promote_ready_verify_requests(
            lineage_id=lineage_id,
            generation=generation,
        )
    return True


def _publish_verify_attempt_result(
    *,
    store: DispatcherStateStore,
    request_id: str,
    attempt_id: str,
    verdict: dict[str, Any],
) -> bool:
    return store.publish_verify_result_if_current(
        request_id=request_id,
        attempt_id=attempt_id,
        result=VerifyResultRecord(
            request_id=request_id,
            attempt_id=attempt_id,
            verdict=verdict,
            terminal_state="succeeded",
        ),
    )


def execute_dispatcher_build_attempt(
    payload_dict: dict[str, Any],
    *,
    redis_conn: Any = None,
) -> dict[str, Any]:
    payload = DispatcherAttemptPayload(**payload_dict)
    store = _get_dispatcher_store(payload.experiment_name, redis_conn=redis_conn)
    build_payload = payload.ci_job_payload or {}
    if "_job_class" in build_payload:
        result_dict = execute_ci_build(build_payload)
    elif "patch" in build_payload:
        result_dict = execute_patch_build(build_payload)
    else:
        raise ValueError(
            "Dispatcher build attempt payload must contain either '_job_class' "
            "or 'patch'"
        )
    _publish_build_attempt_result(
        store=store,
        request_id=payload.request_id,
        attempt_id=payload.attempt_id,
        generation=payload.generation,
        evaluator_id=payload.evaluator_id,
        lineage_id=payload.lineage_id,
        result_dict=result_dict,
    )
    return result_dict


def execute_dispatcher_verify_attempt(
    payload_dict: dict[str, Any],
    *,
    redis_conn: Any = None,
) -> dict[str, Any]:
    payload = DispatcherAttemptPayload(**payload_dict)
    store = _get_dispatcher_store(payload.experiment_name, redis_conn=redis_conn)
    verify_payload = payload.verify_payload or {}
    if "patch" in verify_payload:
        verdict = execute_patch_verify(verify_payload)
    else:
        verdict = verify_single_pov(verify_payload)
    _publish_verify_attempt_result(
        store=store,
        request_id=payload.request_id,
        attempt_id=payload.attempt_id,
        verdict=verdict,
    )
    return verdict
