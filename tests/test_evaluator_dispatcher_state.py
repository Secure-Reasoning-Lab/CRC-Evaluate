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

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hdel(self, key: str, field: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket or field not in bucket:
            return 0
        del bucket[field]
        if not bucket:
            self._hashes.pop(key, None)
        return 1

    def hgetdel(self, key: str, field: str) -> str | None:
        value = self.hget(key, field)
        self.hdel(key, field)
        return value


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
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")

    store.publish_verify_result(
        "req-1",
        VerifyResultRecord(
            request_id="req-1",
            attempt_id="attempt-1",
            verdict={"status": "ok"},
            terminal_state="completed",
        ),
    )

    results, remaining = store.poll_verify_results(["req-1"])
    assert results == [{"status": "ok"}]
    assert remaining == []

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
