"""Tests for evaluator dispatcher runtime helpers."""

from __future__ import annotations

import time


class _FakeRedis:
    """Minimal fake Redis for evaluator dispatcher tests."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return self._hashes.get(key, {}).copy()


def test_heartbeat_upserts_evaluator_presence() -> None:
    from crsbench.distributed.evaluator_dispatcher import heartbeat_evaluator
    from crsbench.distributed.evaluator_dispatcher_state import DispatcherStateStore

    redis_conn = _FakeRedis()

    heartbeat_evaluator(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
        worker_name="eval-1",
    )

    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    assert store.list_live_evaluators(now=time.time() + 1) == ["eval-1"]
