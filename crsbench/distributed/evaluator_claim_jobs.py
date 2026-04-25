"""Claim-worker verify wrappers that publish logical results by request ID."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, cast

from crsbench.distributed.evaluator_jobs import verify_single_pov
from crsbench.distributed.patch_evaluator_jobs import execute_patch_verify
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClaimedVerifyPayload:
    experiment_name: str
    request_id: str
    evaluator_id: str
    request_kind: str
    verify_payload: dict[str, Any]


def _get_verify_claim_store(experiment_name: str, *, redis_conn: Any = None):
    from crsbench.distributed.evaluator_verify_claims import (
        EvaluatorVerifyClaimStore,
    )

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
            raise ValueError("CRSBENCH_REDIS_HOST is required for claim verify jobs")
        redis_conn = create_redis_connection(redis_host)

    return EvaluatorVerifyClaimStore(
        cast("Any", redis_conn), experiment_name=experiment_name
    )


def execute_claimed_verify(
    payload_dict: dict[str, Any],
    *,
    redis_conn: Any = None,
) -> dict[str, Any]:
    """Execute local verify work and publish the logical terminal result."""
    payload = ClaimedVerifyPayload(**payload_dict)
    if payload.request_kind == "patch":
        result = execute_patch_verify(payload.verify_payload)
    else:
        result = verify_single_pov(payload.verify_payload)

    store = _get_verify_claim_store(payload.experiment_name, redis_conn=redis_conn)
    published = store.publish_result_if_current(
        request_id=payload.request_id,
        evaluator_id=payload.evaluator_id,
        now=time.time(),
        result=result,
    )
    if not published:
        logger.warning(
            "Discarded stale logical verify result for request={} evaluator={}",
            payload.request_id,
            payload.evaluator_id,
        )
    return result
