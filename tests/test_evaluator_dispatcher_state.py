"""Tests for evaluator dispatcher state storage."""

from __future__ import annotations

import pytest
from crsbench.distributed.evaluator_dispatcher_state import (
    BuildRequestRecord,
    DispatcherStateStore,
    VerifyRequestRecord,
    VerifyResultRecord,
)


class _FakeRedis:
    """Minimal fake Redis for dispatcher state tests."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self.eval_calls: list[tuple[str, int, tuple[str, ...]]] = []

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return self._hashes.get(key, {}).copy()

    def hdel(self, key: str, field: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket or field not in bucket:
            return 0
        del bucket[field]
        if not bucket:
            self._hashes.pop(key, None)
        return 1

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> list[str | None]:
        self.eval_calls.append((script, numkeys, keys_and_args))
        assert numkeys == 1
        assert keys_and_args

        key = keys_and_args[0]
        fields = keys_and_args[1:]
        results: list[str | None] = []
        for field in fields:
            value = self.hget(key, field)
            if value is not None:
                self.hdel(key, field)
            results.append(value)
        return results


def test_submit_and_load_verify_request() -> None:
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")
    record = VerifyRequestRecord(
        request_id="req-1",
        trial_id="trial-1",
        benchmark="bench-1",
        harness="h-1",
        pov_id="pov-1",
        owner_key="owner-1",
        lineage_id="lineage-1",
        generation=2,
        state="queued",
        build_request_ids=["build-1"],
        payload={"trial_id": "trial-1"},
    )

    request_id = store.submit_verify_request(record)
    assert request_id == "req-1"
    loaded = store.load_verify_request("req-1")

    assert loaded is not None
    assert loaded.request_id == "req-1"
    assert loaded.payload == {"trial_id": "trial-1"}


def test_submit_and_load_build_request() -> None:
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")
    record = BuildRequestRecord(
        request_id="build-1",
        trial_id="trial-1",
        benchmark="bench-1",
        owner_key="owner-1",
        lineage_id="lineage-1",
        generation=1,
        state="queued",
        payload={"variant": "v1"},
    )

    request_id = store.submit_build_request(record)
    assert request_id == "build-1"
    loaded = store.load_build_request("build-1")
    assert loaded is not None
    assert loaded.request_id == "build-1"
    assert loaded.payload == {"variant": "v1"}


def test_publish_and_poll_verify_results() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-1")

    store.publish_verify_result(
        "req-1",
        VerifyResultRecord(
            request_id="req-1",
            attempt_id="attempt-1",
            verdict={"status": "ok"},
            terminal_state="completed",
        ),
    )
    store.publish_verify_result(
        "req-2",
        VerifyResultRecord(
            request_id="req-2",
            attempt_id="attempt-2",
            verdict={"status": "error"},
            terminal_state="failed",
        ),
    )

    results, remaining = store.poll_verify_results(["req-1", "req-2", "req-3"])
    assert results == [{"status": "ok"}, {"status": "error"}]
    assert remaining == ["req-3"]
    assert len(redis_conn.eval_calls) == 1

    results, remaining = store.poll_verify_results(["req-1"])
    assert results == []
    assert remaining == ["req-1"]


def test_publish_verify_result_rejects_mismatched_request_id() -> None:
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")

    with pytest.raises(ValueError, match="request_id does not match"):
        store.publish_verify_result(
            "req-1",
            VerifyResultRecord(
                request_id="req-2",
                attempt_id="attempt-1",
                verdict={"status": "ok"},
                terminal_state="completed",
            ),
        )


def test_dispatcher_state_store_rejects_invalid_experiment_name() -> None:
    with pytest.raises(ValueError, match="Invalid name for queue component"):
        DispatcherStateStore(_FakeRedis(), experiment_name="exp 1")


def test_poll_verify_results_rejects_eval_length_mismatch() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-1")

    redis_conn.eval_calls.clear()

    def _short_eval(script: str, numkeys: int, *keys_and_args: str) -> list[str | None]:
        redis_conn.eval_calls.append((script, numkeys, keys_and_args))
        return [None]

    redis_conn.eval = _short_eval  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="dispatcher verify poll returned 1 results"):
        store.poll_verify_results(["req-1", "req-2"])
