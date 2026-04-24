"""Tests for evaluator dispatcher state storage."""

from __future__ import annotations

import threading

import pytest
from crsbench.distributed.evaluator_dispatcher_state import (
    BuildRequestRecord,
    BuildResultRecord,
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


class _LeaseExpiryBoundaryRedis(_FakeRedis):
    """Fake Redis that exposes split-brain if lease acquisition is not atomic."""

    def __init__(self) -> None:
        super().__init__()
        self._read_barrier = threading.Barrier(2)
        self._eval_lock = threading.Lock()

    def hget(self, key: str, field: str) -> str | None:
        value = super().hget(key, field)
        if key.endswith(":lease") and field == "expires_at":
            self._read_barrier.wait(timeout=1.0)
        return value

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        with self._eval_lock:
            assert numkeys == 1
            lease_key = keys_and_args[0]
            evaluator_id = keys_and_args[1]
            now = float(keys_and_args[2])
            ttl_seconds = float(keys_and_args[3])
            current_holder = super().hget(lease_key, "holder")
            current_expires = super().hget(lease_key, "expires_at")
            if current_holder is not None and current_expires is not None:
                if float(current_expires) > now and current_holder != evaluator_id:
                    return 0
            super().hset(lease_key, "holder", evaluator_id)
            super().hset(lease_key, "expires_at", str(now + ttl_seconds))
            return 1


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


def test_try_acquire_dispatcher_lease_is_atomic_across_expiry_boundary() -> None:
    redis_conn = _LeaseExpiryBoundaryRedis()
    lease_key = "crsbench:dispatcher:exp-1:lease"
    redis_conn.hset(lease_key, "holder", "stale-eval")
    redis_conn.hset(lease_key, "expires_at", "1.0")

    first = DispatcherStateStore(redis_conn, experiment_name="exp-1")
    second = DispatcherStateStore(redis_conn, experiment_name="exp-1")
    outcomes: dict[str, bool] = {}

    def _attempt(store: DispatcherStateStore, evaluator_id: str) -> None:
        outcomes[evaluator_id] = store.try_acquire_dispatcher_lease(
            evaluator_id,
            now=10.0,
        )

    threads = [
        threading.Thread(target=_attempt, args=(first, "eval-1")),
        threading.Thread(target=_attempt, args=(second, "eval-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes.values()) == [False, True]
    winner = next(
        evaluator_id for evaluator_id, acquired in outcomes.items() if acquired
    )
    assert redis_conn.hget(lease_key, "holder") == winner


def test_build_attempt_is_current_decodes_redis_bytes() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-1")
    request_id = "build-1"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=1,
            state="queued",
            payload={"variant": "v1"},
        )
    )
    store.assign_build_attempt(
        request_id=request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-1",
        generation=1,
    )

    original_hget = redis_conn.hget

    def _bytes_hget(key: str, field: str) -> str | bytes | None:
        value = original_hget(key, field)
        if value is None:
            return None
        return value.encode("utf-8")

    redis_conn.hget = _bytes_hget  # type: ignore[method-assign]

    assert store.build_attempt_is_current(request_id, "attempt-1")


def test_verify_attempt_is_current_decodes_redis_bytes() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-1")
    request_id = "verify-1"
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            harness="h-1",
            pov_id="pov-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=1,
            state="ready",
            build_request_ids=[],
            payload={"trial_id": "trial-1"},
        )
    )
    store.assign_verify_attempt(
        request_id=request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-1",
        generation=1,
    )

    original_hget = redis_conn.hget

    def _bytes_hget(key: str, field: str) -> str | bytes | None:
        value = original_hget(key, field)
        if value is None:
            return None
        return value.encode("utf-8")

    redis_conn.hget = _bytes_hget  # type: ignore[method-assign]

    assert store.verify_attempt_is_current(request_id, "attempt-1")


def test_required_build_request_ids_only_include_unfinished_blocked_verify_deps() -> (
    None
):
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")
    build_request_id = "build-1"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=build_request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=1,
            state="ready",
            payload={"variant": "v1"},
        )
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id="verify-1",
            trial_id="trial-1",
            benchmark="bench-1",
            harness="h-1",
            pov_id="pov-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=1,
            state="blocked_on_build",
            build_request_ids=[build_request_id],
            payload={"trial_id": "trial-1"},
        )
    )

    assert store.required_build_request_ids() == {build_request_id}
    assert store.has_pending_required_builds() is True


def test_required_build_request_ids_clear_after_build_success_and_verify_promotion() -> (
    None
):
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")
    build_request_id = "build-1"
    lineage_id = "lineage-1"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=build_request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            owner_key="owner-1",
            lineage_id=lineage_id,
            generation=1,
            state="ready",
            payload={"variant": "v1"},
        )
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id="verify-1",
            trial_id="trial-1",
            benchmark="bench-1",
            harness="h-1",
            pov_id="pov-1",
            owner_key="owner-1",
            lineage_id=lineage_id,
            generation=1,
            state="blocked_on_build",
            build_request_ids=[build_request_id],
            payload={"trial_id": "trial-1"},
        )
    )
    store.publish_build_result(
        build_request_id,
        BuildResultRecord(
            request_id=build_request_id,
            attempt_id="attempt-1",
            generation=1,
            evaluator_id="eval-1",
            terminal_state="succeeded",
        ),
    )

    store.promote_ready_verify_requests(lineage_id=lineage_id, generation=1)
    promoted = store.load_verify_request("verify-1")

    assert promoted is not None
    assert promoted.state == "ready"
    assert store.has_pending_required_builds() is False


def test_required_build_request_ids_skip_missing_and_require_failed_or_mismatched() -> (
    None
):
    store = DispatcherStateStore(_FakeRedis(), experiment_name="exp-1")
    missing_request_id = "build-missing"
    mismatched_request_id = "build-mismatched"
    failed_request_id = "build-failed"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=mismatched_request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=2,
            state="ready",
            payload={"variant": "v1"},
        )
    )
    store.submit_build_request(
        BuildRequestRecord(
            request_id=failed_request_id,
            trial_id="trial-1",
            benchmark="bench-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=1,
            state="ready",
            payload={"variant": "v1"},
        )
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id="verify-1",
            trial_id="trial-1",
            benchmark="bench-1",
            harness="h-1",
            pov_id="pov-1",
            owner_key="owner-1",
            lineage_id="lineage-1",
            generation=2,
            state="blocked_on_build",
            build_request_ids=[
                missing_request_id,
                mismatched_request_id,
                failed_request_id,
            ],
            payload={"trial_id": "trial-1"},
        )
    )
    store.publish_build_result(
        mismatched_request_id,
        BuildResultRecord(
            request_id=mismatched_request_id,
            attempt_id="attempt-1",
            generation=1,
            evaluator_id="eval-1",
            terminal_state="succeeded",
        ),
    )
    store.publish_build_result(
        failed_request_id,
        BuildResultRecord(
            request_id=failed_request_id,
            attempt_id="attempt-1",
            generation=1,
            evaluator_id="eval-1",
            terminal_state="failed",
        ),
    )

    assert store.required_build_request_ids() == {
        failed_request_id,
        mismatched_request_id,
    }
