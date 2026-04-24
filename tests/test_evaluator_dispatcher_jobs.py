"""Tests for evaluator dispatcher job wrappers."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from crsbench.distributed.evaluator_dispatcher_state import (
    BuildRequestRecord,
    DispatcherStateStore,
    VerifyRequestRecord,
)


class _FakeRedis:
    """Minimal fake Redis for dispatcher job tests."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

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

    def eval(
        self, script: str, numkeys: int, *keys_and_args: str
    ) -> int | list[str | None]:
        assert keys_and_args
        if numkeys == 1:
            key = keys_and_args[0]
            fields = keys_and_args[1:]
            results: list[str | None] = []
            for field in fields:
                value = self.hget(key, field)
                if value is not None:
                    self.hdel(key, field)
                results.append(value)
            return results
        if numkeys == 2:
            attempts_key, results_key = keys_and_args[:2]
            request_id, expected_attempt_id, result_payload = keys_and_args[2:]
            current_attempt_id = self.hget(attempts_key, request_id)
            if current_attempt_id != expected_attempt_id:
                return 0
            self.hset(results_key, request_id, result_payload)
            return 1
        raise AssertionError(f"unexpected eval call: numkeys={numkeys}")


class _RacingPublishRedis(_FakeRedis):
    """Fake Redis that simulates a new attempt winning right before CAS publish."""

    def __init__(self, replacement_attempt_id: str) -> None:
        super().__init__()
        self.replacement_attempt_id = replacement_attempt_id

    def eval(
        self, script: str, numkeys: int, *keys_and_args: str
    ) -> int | list[str | None]:
        if numkeys == 2:
            attempts_key, results_key = keys_and_args[:2]
            request_id, expected_attempt_id, result_payload = keys_and_args[2:]
            self.hset(attempts_key, request_id, self.replacement_attempt_id)
            return super().eval(
                script,
                numkeys,
                attempts_key,
                results_key,
                request_id,
                expected_attempt_id,
                result_payload,
            )
        return super().eval(script, numkeys, *keys_and_args)


def test_execute_dispatcher_build_attempt_publishes_build_result_and_promotes_verify() -> (
    None
):
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        execute_dispatcher_build_attempt,
    )

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    build_request_id = "build:trial-1:bench:0"
    verify_request_id = "verify:trial-1:bench:h1:pov-1"
    lineage_id = "bench::address::pkgs::inc"

    store.submit_build_request(
        BuildRequestRecord(
            request_id=build_request_id,
            trial_id="trial-1",
            benchmark="bench",
            owner_key="ownerA",
            lineage_id=lineage_id,
            generation=1,
            state="ready",
            payload={"_job_class": "BuildSingleVariantJob"},
        )
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=verify_request_id,
            trial_id="trial-1",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id=lineage_id,
            generation=1,
            state="blocked_on_build",
            build_request_ids=[build_request_id],
            payload={"trial_id": "trial-1"},
        )
    )
    store.assign_build_attempt(
        request_id=build_request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-build-1",
        generation=1,
    )
    with patch(
        "crsbench.distributed.evaluator_dispatcher_jobs.execute_ci_build",
        return_value={"job_id": build_request_id, "success": True},
    ):
        result = execute_dispatcher_build_attempt(
            {
                "experiment_name": "exp-test",
                "request_id": build_request_id,
                "attempt_id": "attempt-build-1",
                "evaluator_id": "eval-1",
                "generation": 1,
                "lineage_id": lineage_id,
                "ci_job_payload": {"_job_class": "BuildSingleVariantJob"},
            },
            redis_conn=redis_conn,
        )

    assert result["success"] is True
    build_result = store.load_build_result(build_request_id)
    assert build_result is not None
    assert build_result.terminal_state == "succeeded"
    verify_request = store.load_verify_request(verify_request_id)
    assert verify_request is not None
    assert verify_request.state == "ready"


def test_execute_dispatcher_verify_attempt_publishes_terminal_result(
    monkeypatch,
) -> None:
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        execute_dispatcher_verify_attempt,
    )

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
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
    monkeypatch.setattr(
        "crsbench.distributed.evaluator_dispatcher_jobs.verify_single_pov",
        lambda _payload: {
            "trial_id": "trial-1",
            "benchmark": "bench",
            "harness": "h1",
            "verdict": {"pov_id": "pov-1", "status": "cpv", "triggered_bug": True},
            "completed_at": 1.0,
        },
    )

    verdict = execute_dispatcher_verify_attempt(
        {
            "experiment_name": "exp-test",
            "request_id": request_id,
            "attempt_id": "attempt-1",
            "evaluator_id": "eval-1",
            "generation": 1,
            "lineage_id": "bench::address::pkgs::inc",
            "verify_payload": {"trial_id": "trial-1"},
        },
        redis_conn=redis_conn,
    )

    assert verdict["verdict"]["status"] == "cpv"
    completed, remaining = store.poll_verify_results([request_id])
    assert remaining == []
    assert completed[0]["verdict"]["status"] == "cpv"


def test_stale_verify_attempt_cannot_publish() -> None:
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        _publish_verify_attempt_result,
    )

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            build_request_ids=[],
            payload={"trial_id": "trial-1"},
        )
    )
    store.assign_verify_attempt(
        request_id=request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-2",
        generation=1,
    )

    assert not _publish_verify_attempt_result(
        store=store,
        request_id=request_id,
        attempt_id="attempt-1",
        verdict={
            "trial_id": "trial-1",
            "benchmark": "bench",
            "harness": "h1",
            "verdict": {"pov_id": "pov-1", "status": "error", "triggered_bug": False},
            "completed_at": 1.0,
        },
    )
    completed, remaining = store.poll_verify_results([request_id])
    assert completed == []
    assert remaining == [request_id]


def test_stale_build_attempt_cannot_publish() -> None:
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        _publish_build_attempt_result,
    )

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "build:trial-1:bench:0"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            payload={"_job_class": "BuildSingleVariantJob"},
        )
    )
    store.assign_build_attempt(
        request_id=request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-build-2",
        generation=1,
    )

    assert not _publish_build_attempt_result(
        store=store,
        request_id=request_id,
        attempt_id="attempt-build-1",
        generation=1,
        evaluator_id="eval-1",
        lineage_id="bench::address::pkgs::inc",
        result_dict={"job_id": request_id, "success": True},
    )
    assert store.load_build_result(request_id) is None


def test_verify_request_rejects_conflicting_lineage() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            build_request_ids=[],
            payload={"trial_id": "trial-1"},
        )
    )

    with pytest.raises(ValueError, match="conflicting verify request identity"):
        store.submit_verify_request(
            VerifyRequestRecord(
                request_id=request_id,
                trial_id="trial-1",
                benchmark="bench",
                harness="h1",
                pov_id="pov-1",
                owner_key="ownerA",
                lineage_id="bench::ubsan::pkgs::inc",
                generation=1,
                state="ready",
                build_request_ids=[],
                payload={"trial_id": "trial-1"},
            )
        )


def test_build_request_rejects_conflicting_lineage() -> None:
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "build:trial-1:bench:0"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            payload={"_job_class": "BuildSingleVariantJob"},
        )
    )

    with pytest.raises(ValueError, match="conflicting build request identity"):
        store.submit_build_request(
            BuildRequestRecord(
                request_id=request_id,
                trial_id="trial-1",
                benchmark="bench",
                owner_key="ownerA",
                lineage_id="bench::ubsan::pkgs::inc",
                generation=1,
                state="ready",
                payload={"_job_class": "BuildSingleVariantJob"},
            )
        )


def test_build_publish_is_atomic_against_attempt_reassignment() -> None:
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        _publish_build_attempt_result,
    )

    redis_conn = _RacingPublishRedis(replacement_attempt_id="attempt-build-2")
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "build:trial-1:bench:0"
    store.submit_build_request(
        BuildRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            payload={"_job_class": "BuildSingleVariantJob"},
        )
    )
    store.assign_build_attempt(
        request_id=request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-build-1",
        generation=1,
    )

    assert not _publish_build_attempt_result(
        store=store,
        request_id=request_id,
        attempt_id="attempt-build-1",
        generation=1,
        evaluator_id="eval-1",
        lineage_id="bench::address::pkgs::inc",
        result_dict={"job_id": request_id, "success": True},
    )
    assert store.load_build_result(request_id) is None


def test_verify_publish_is_atomic_against_attempt_reassignment() -> None:
    from crsbench.distributed.evaluator_dispatcher_jobs import (
        _publish_verify_attempt_result,
    )

    redis_conn = _RacingPublishRedis(replacement_attempt_id="attempt-2")
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=request_id,
            trial_id="trial-1",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
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

    assert not _publish_verify_attempt_result(
        store=store,
        request_id=request_id,
        attempt_id="attempt-1",
        verdict={
            "trial_id": "trial-1",
            "benchmark": "bench",
            "harness": "h1",
            "verdict": {"pov_id": "pov-1", "status": "error", "triggered_bug": False},
            "completed_at": 1.0,
        },
    )
    completed, remaining = store.poll_verify_results([request_id])
    assert completed == []
    assert remaining == [request_id]
