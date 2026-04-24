"""Tests for claim-worker verify wrappers."""

from __future__ import annotations

from unittest.mock import patch

from crsbench.distributed.evaluator_verify_claims import (
    EvaluatorVerifyClaimStore,
    VerifyRequestRecord,
)


class _FakePipeline:
    def __init__(self, redis_conn: "_FakeRedis") -> None:
        self._redis = redis_conn
        self._commands: list[tuple[str, str, str, str]] = []

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.reset()

    def watch(self, *keys: str) -> None:
        del keys

    def unwatch(self) -> None:
        return None

    def hget(self, key: str, field: str) -> str | None:
        return self._redis.hget(key, field)

    def hgetall(self, key: str) -> dict[str, str]:
        return self._redis.hgetall(key)

    def multi(self) -> None:
        return None

    def hset(self, key: str, field: str, value: str) -> None:
        self._commands.append(("hset", key, field, value))

    def execute(self) -> list[int]:
        results = []
        for op, key, field, value in self._commands:
            if op == "hset":
                results.append(self._redis.hset(key, field, value))
        self.reset()
        return results

    def reset(self) -> None:
        self._commands = []


class _FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def hset(self, key: str, field: str, value: str) -> int:
        self._hashes.setdefault(key, {})[field] = value
        return 1

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


def test_execute_claimed_verify_publishes_pov_result_for_current_claim() -> None:
    from crsbench.distributed.evaluator_claim_jobs import execute_claimed_verify

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"trial_id": "trial-1"},
        )
    )
    assert store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=10,
    )

    with (
        patch(
            "crsbench.distributed.evaluator_claim_jobs.verify_single_pov",
            return_value={"status": "cpv"},
        ),
        patch(
            "crsbench.distributed.evaluator_claim_jobs.time.time", return_value=105.0
        ),
    ):
        result = execute_claimed_verify(
            {
                "experiment_name": "exp1",
                "request_id": request_id,
                "evaluator_id": "eval-1",
                "request_kind": "pov",
                "verify_payload": {"trial_id": "trial-1"},
            },
            redis_conn=redis_conn,
        )

    assert result == {"status": "cpv"}
    record = store.load_request(request_id)
    assert record is not None
    assert record.terminal_result == {"status": "cpv"}


def test_execute_claimed_verify_discards_stale_result_after_claim_expiry() -> None:
    from crsbench.distributed.evaluator_claim_jobs import execute_claimed_verify

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"trial_id": "trial-1"},
        )
    )
    assert store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=5,
    )

    with (
        patch(
            "crsbench.distributed.evaluator_claim_jobs.verify_single_pov",
            return_value={"status": "cpv"},
        ),
        patch(
            "crsbench.distributed.evaluator_claim_jobs.time.time", return_value=106.0
        ),
    ):
        result = execute_claimed_verify(
            {
                "experiment_name": "exp1",
                "request_id": request_id,
                "evaluator_id": "eval-1",
                "request_kind": "pov",
                "verify_payload": {"trial_id": "trial-1"},
            },
            redis_conn=redis_conn,
        )

    assert result == {"status": "cpv"}
    record = store.load_request(request_id)
    assert record is not None
    assert record.terminal_result is None
