"""Tests for dispatcher-aware verify queue behavior."""

from __future__ import annotations

from unittest.mock import patch

import crsbench.distributed.verify_queue as verify_queue
import pytest
from crsbench.distributed.evaluator_dispatcher_state import (
    DispatcherStateStore,
    VerifyResultRecord,
)
from crsbench.distributed.queue import (
    EVALUATOR_ROUTING_MODEL_ENV,
    ROUTING_MODEL_DISPATCHER,
)


class _FakeRedis:
    """Minimal fake Redis for dispatcher verify queue tests."""

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

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> list[str | None]:
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


def test_submit_async_build_requests_dispatcher(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)
    redis_conn = _FakeRedis()

    build_payloads = [{"variant": "v1"}, {"variant": "v2"}]
    request_ids = verify_queue.submit_async_build_requests(
        redis_host=None,
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="bench",
        build_payloads=build_payloads,
        sanitizer="address",
        source_mode="pkgs",
        use_inc_build=True,
        redis_conn=redis_conn,
    )

    assert request_ids == ["build:trial-1:bench:0", "build:trial-1:bench:1"]
    store = DispatcherStateStore(redis_conn, experiment_name="exp1")
    record = store.load_build_request("build:trial-1:bench:0")

    assert record is not None
    assert record.owner_key == "trial::exp1::trial-1"
    assert record.lineage_id == "bench::address::pkgs::inc"
    assert record.generation == 1
    assert record.state == "ready"
    assert record.payload == build_payloads[0]


def test_submit_async_build_requests_dispatcher_requires_trial_id(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)

    with pytest.raises(ValueError, match="trial_id is required"):
        verify_queue.submit_async_build_requests(
            redis_host=None,
            experiment_name="exp1",
            trial_id="",
            benchmark="bench",
            build_payloads=[{"variant": "v1"}],
            sanitizer="address",
            source_mode="pkgs",
            use_inc_build=True,
            redis_conn=_FakeRedis(),
        )


def test_enqueue_single_pov_dispatcher_submits_verify_request(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)
    redis_conn = _FakeRedis()

    job_id = verify_queue.enqueue_single_pov(
        verify_queue=None,
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="bench",
        harness="h",
        pov_id="pov-1",
        pov_data=b"pov-data",
        sanitizer="address",
        build_job_ids=["build-1"],
        source_mode="pkgs",
        use_inc_build=False,
        redis_conn=redis_conn,
    )

    assert job_id == "verify:trial-1:bench:h:pov-1"
    store = DispatcherStateStore(redis_conn, experiment_name="exp1")
    record = store.load_verify_request(job_id)

    assert record is not None
    assert record.owner_key == "trial::exp1::trial-1"
    assert record.lineage_id == "bench::address::pkgs::clean"
    assert record.generation == 1
    assert record.state == "blocked_on_build"
    assert record.build_request_ids == ["build-1"]
    assert record.payload["trial_id"] == "trial-1"
    assert record.payload["pov"]["pov_id"] == "pov-1"


def test_enqueue_single_pov_dispatcher_without_builds_is_ready(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)
    redis_conn = _FakeRedis()

    job_id = verify_queue.enqueue_single_pov(
        verify_queue=None,
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="bench",
        harness="h",
        pov_id="pov-1",
        pov_data=b"pov-data",
        redis_conn=redis_conn,
    )

    assert job_id == "verify:trial-1:bench:h:pov-1"
    store = DispatcherStateStore(redis_conn, experiment_name="exp1")
    record = store.load_verify_request(job_id)

    assert record is not None
    assert record.state == "ready"
    assert record.build_request_ids == []


def test_enqueue_single_pov_dispatcher_requires_trial_id(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)

    job_id = verify_queue.enqueue_single_pov(
        verify_queue=None,
        experiment_name="exp1",
        trial_id="",
        benchmark="bench",
        harness="h",
        pov_id="pov-1",
        pov_data=b"pov-data",
        redis_conn=_FakeRedis(),
    )

    assert job_id is None


def test_poll_single_pov_verdicts_dispatcher(monkeypatch) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)
    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp1")

    request_id = "verify:trial-1:bench:h:pov-1"
    store.publish_verify_result(
        request_id,
        VerifyResultRecord(
            request_id=request_id,
            attempt_id="attempt-1",
            verdict={
                "pov_id": "pov-1",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {},
                "error": None,
            },
            terminal_state="completed",
        ),
    )

    with patch("crsbench.distributed.verify_queue.time.time", return_value=123.0):
        completed, remaining = verify_queue.poll_single_pov_verdicts(
            "redis.local",
            [request_id],
            experiment_name="exp1",
            redis_conn=redis_conn,
        )

    assert completed == [
        {
            "trial_id": "trial-1",
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": "pov-1",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {},
                "error": None,
            },
            "completed_at": 123.0,
        }
    ]
    assert remaining == []


def test_poll_single_pov_verdicts_skips_dispatcher_without_experiment(
    monkeypatch,
) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)
    monkeypatch.setattr(verify_queue, "REDIS_AVAILABLE", False)

    with patch.object(
        verify_queue, "_get_dispatcher_store", side_effect=AssertionError
    ):
        completed, remaining = verify_queue.poll_single_pov_verdicts(
            "redis.local",
            ["verify:trial-1:bench:h:pov-1"],
        )

    assert completed == []
    assert remaining == ["verify:trial-1:bench:h:pov-1"]


@patch("crsbench.distributed.queue.create_redis_connection")
@patch("crsbench.distributed.verify_queue.rq")
def test_poll_single_pov_verdicts_dispatcher_non_logical_ids_use_shared_path(
    mock_rq,
    mock_create_redis_connection,
    monkeypatch,
) -> None:
    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)

    redis_conn = object()
    mock_create_redis_connection.return_value = redis_conn
    job = mock_rq.job.Job.fetch.return_value
    job.get_status.return_value = "finished"
    job.result = {"status": "ok"}

    completed, remaining = verify_queue.poll_single_pov_verdicts(
        "redis.local",
        ["rq-job-1"],
        experiment_name="exp1",
    )

    assert completed == [{"status": "ok"}]
    assert remaining == []
