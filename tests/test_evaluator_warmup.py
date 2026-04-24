"""Tests for dispatcher-local evaluator warmup feeder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crsbench.distributed.evaluator_warmup import (
    DispatcherWarmupFeeder,
    WarmupBuildSpec,
)


@dataclass
class _FakeRegistry:
    count: int


class _FakeIntermediateQueue:
    def __init__(self, job_ids: list[str]) -> None:
        self._job_ids = list(job_ids)

    def get_job_ids(self) -> list[str]:
        return list(self._job_ids)


class _FakeQueue:
    def __init__(
        self,
        *,
        queued: int = 0,
        intermediate_job_ids: list[str] | None = None,
        started: int = 0,
    ) -> None:
        self.count = queued
        self.intermediate_queue = _FakeIntermediateQueue(intermediate_job_ids or [])
        self.started_job_registry = _FakeRegistry(count=started)
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(
        self,
        func_name: str,
        payload: dict[str, Any],
        *,
        job_timeout: int,
        result_ttl: int,
        job_id: str,
        meta: dict[str, Any],
    ) -> None:
        self.enqueued.append(
            {
                "func_name": func_name,
                "payload": payload,
                "job_timeout": job_timeout,
                "result_ttl": result_ttl,
                "job_id": job_id,
                "meta": meta,
            }
        )


class _FakeStateStore:
    def __init__(self, *, pending_required_builds: bool) -> None:
        self._pending_required_builds = pending_required_builds

    def has_pending_required_builds(self) -> bool:
        return self._pending_required_builds


def _warmup_specs(count: int) -> list[WarmupBuildSpec]:
    return [
        WarmupBuildSpec(
            job_id=f"warmup-build-{index}",
            payload={"id": index},
            meta={"experiment_name": "exp-test"},
        )
        for index in range(count)
    ]


def test_feeder_enqueues_when_no_required_demand_and_spare_capacity_exists() -> None:
    queue = _FakeQueue(queued=1, intermediate_job_ids=[], started=0)
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=_warmup_specs(3),
        build_capacity=3,
    )

    enqueued = feeder.tick()

    assert enqueued == 2
    assert [entry["job_id"] for entry in queue.enqueued] == [
        "warmup-build-0",
        "warmup-build-1",
    ]


def test_feeder_stops_when_required_demand_exists() -> None:
    queue = _FakeQueue(queued=0, intermediate_job_ids=[], started=0)
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=True),
        warmup_specs=_warmup_specs(2),
        build_capacity=2,
    )

    enqueued = feeder.tick()

    assert enqueued == 0
    assert queue.enqueued == []


def test_feeder_respects_running_claimed_and_queued_capacity() -> None:
    queue = _FakeQueue(queued=1, intermediate_job_ids=["claim-a", "claim-b"], started=1)
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=_warmup_specs(4),
        build_capacity=5,
    )

    enqueued = feeder.tick()

    assert enqueued == 1
    assert [entry["job_id"] for entry in queue.enqueued] == ["warmup-build-0"]
