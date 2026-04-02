"""Runtime helpers for cloud-backed worker readiness reporting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from crsbench.cloud.readiness import (
    CloudInstanceRole,
    CloudReadinessStore,
    CloudWorkerState,
    CloudWorkerStatus,
    ReadinessRedisProtocol,
)
from crsbench.distributed.queue import create_redis_connection

CRSBENCH_CLOUD_EXPERIMENT_ENV = "CRSBENCH_CLOUD_EXPERIMENT"
CRSBENCH_CLOUD_INSTANCE_ID_ENV = "CRSBENCH_CLOUD_INSTANCE_ID"
CRSBENCH_CLOUD_INSTANCE_NAME_ENV = "CRSBENCH_CLOUD_INSTANCE_NAME"
CRSBENCH_CLOUD_ZONE_ENV = "CRSBENCH_CLOUD_ZONE"
CRSBENCH_CLOUD_ROLE_ENV = "CRSBENCH_CLOUD_ROLE"


@dataclass(frozen=True)
class CloudWorkerRuntimeContext:
    """Cloud worker identity injected into runtime env by the bootstrap path."""

    experiment_name: str
    instance_id: str
    instance_name: str
    zone: str
    role: CloudInstanceRole = CloudInstanceRole.WORKER

    @classmethod
    def from_env(cls) -> CloudWorkerRuntimeContext | None:
        experiment_name = os.environ.get(CRSBENCH_CLOUD_EXPERIMENT_ENV)
        instance_id = os.environ.get(CRSBENCH_CLOUD_INSTANCE_ID_ENV)
        instance_name = os.environ.get(CRSBENCH_CLOUD_INSTANCE_NAME_ENV)
        zone = os.environ.get(CRSBENCH_CLOUD_ZONE_ENV)
        role = os.environ.get(CRSBENCH_CLOUD_ROLE_ENV, CloudInstanceRole.WORKER)
        if not all((experiment_name, instance_id, instance_name, zone)):
            return None
        return cls(
            experiment_name=cast("str", experiment_name),
            instance_id=cast("str", instance_id),
            instance_name=cast("str", instance_name),
            zone=cast("str", zone),
            role=CloudInstanceRole(role),
        )


def report_cloud_worker_state_from_env(
    *,
    redis_host: str,
    state: CloudWorkerState | str,
    detail: str | None = None,
    startup_evidence: str | None = None,
) -> bool:
    """Best-effort readiness update using cloud worker identity from env."""
    context = CloudWorkerRuntimeContext.from_env()
    if context is None:
        return False

    state_value = (
        state if isinstance(state, CloudWorkerState) else CloudWorkerState(state)
    )
    connection = create_redis_connection(redis_host)
    store = CloudReadinessStore(cast("ReadinessRedisProtocol", connection))
    try:
        store.record(
            CloudWorkerStatus(
                experiment_name=context.experiment_name,
                instance_id=context.instance_id,
                instance_name=context.instance_name,
                zone=context.zone,
                role=context.role,
                state=state_value,
                detail=detail,
                startup_evidence=startup_evidence,
            )
        )
    except ValueError:
        return False
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return True
