"""Tests for evaluator dispatcher runtime helpers."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from crsbench.distributed.evaluator_dispatcher_state import (
    BuildRequestRecord,
    DispatcherStateStore,
    VerifyRequestRecord,
)


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

    def hdel(self, key: str, field: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket or field not in bucket:
            return 0
        del bucket[field]
        if not bucket:
            self._hashes.pop(key, None)
        return 1

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        assert numkeys == 1
        lease_key = keys_and_args[0]
        evaluator_id = keys_and_args[1]
        now = float(keys_and_args[2])
        ttl_seconds = float(keys_and_args[3])
        current_holder = self.hget(lease_key, "holder")
        current_expires = self.hget(lease_key, "expires_at")
        if current_holder is not None and current_expires is not None:
            if float(current_expires) > now and current_holder != evaluator_id:
                return 0
        self.hset(lease_key, "holder", evaluator_id)
        self.hset(lease_key, "expires_at", str(now + ttl_seconds))
        return 1


class _LeaseCompetingHolderRedis(_FakeRedis):
    """Fake Redis that injects a competing current holder before lease evaluation."""

    def __init__(self) -> None:
        super().__init__()
        self._injected = False

    def _inject_competing_holder(self, lease_key: str) -> None:
        if self._injected:
            return
        self.hset(lease_key, "holder", "eval-2")
        self.hset(lease_key, "expires_at", "60.0")
        self._injected = True

    def hget(self, key: str, field: str) -> str | None:
        if key.endswith(":lease") and field == "expires_at":
            self._inject_competing_holder(key)
        return super().hget(key, field)

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        if numkeys == 1:
            self._inject_competing_holder(keys_and_args[0])
        return super().eval(script, numkeys, *keys_and_args)


def test_heartbeat_upserts_evaluator_presence() -> None:
    from crsbench.distributed.evaluator_dispatcher import heartbeat_evaluator

    redis_conn = _FakeRedis()

    heartbeat_evaluator(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
        worker_name="eval-1",
    )

    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    assert store.list_live_evaluators(now=time.time() + 1) == ["eval-1"]


def test_dispatcher_round_robins_ready_verify_requests_across_owners() -> None:
    from crsbench.distributed.evaluator_dispatcher import EvaluatorDispatcher

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id="verify:trial-a:bench:h1:pov-1",
            trial_id="trial-a",
            benchmark="bench",
            harness="h1",
            pov_id="pov-1",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            build_request_ids=[],
            payload={},
        )
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id="verify:trial-b:bench:h1:pov-2",
            trial_id="trial-b",
            benchmark="bench",
            harness="h1",
            pov_id="pov-2",
            owner_key="ownerB",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            build_request_ids=[],
            payload={},
        )
    )

    dispatcher = EvaluatorDispatcher(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
    )

    first = dispatcher._choose_next_verify_request()
    second = dispatcher._choose_next_verify_request()

    assert first is not None
    assert second is not None
    assert first.owner_key != second.owner_key


def test_dispatcher_leader_lease_rejects_injected_competing_current_holder() -> None:
    from crsbench.distributed.evaluator_dispatcher import EvaluatorDispatcher

    redis_conn = _LeaseCompetingHolderRedis()
    dispatcher = EvaluatorDispatcher(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
    )

    assert not dispatcher.try_acquire_leader_lease(now=0.0)
    assert redis_conn.hget("crsbench:dispatcher:exp-test:lease", "holder") == "eval-2"


def test_dispatcher_dispatches_build_requests_to_local_build_queue() -> None:
    from crsbench.distributed.evaluator_dispatcher import EvaluatorDispatcher

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    store.upsert_evaluator(
        evaluator_id="eval-1",
        worker_name="eval-1",
        expires_in_seconds=60,
    )
    store.submit_build_request(
        BuildRequestRecord(
            request_id="build:trial-1:bench:0",
            trial_id="trial-1",
            benchmark="bench",
            owner_key="ownerA",
            lineage_id="bench::address::pkgs::inc",
            generation=1,
            state="ready",
            payload={"_job_class": "BuildSingleVariantJob"},
        )
    )

    dispatcher = EvaluatorDispatcher(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
    )

    with patch("crsbench.distributed.evaluator_dispatcher.rq.Queue") as mock_queue_cls:
        queue = MagicMock()
        mock_queue_cls.return_value = queue

        dispatched = dispatcher.dispatch_one_build(now=time.time())

    assert dispatched is not None
    queue.enqueue.assert_called_once()


def test_dead_evaluator_reblocks_requests_and_advances_generation() -> None:
    from crsbench.distributed.evaluator_dispatcher import EvaluatorDispatcher

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
    store.assign_build_attempt(
        request_id=build_request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-build-1",
        generation=1,
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
            state="ready",
            build_request_ids=[build_request_id],
            payload={"trial_id": "trial-1"},
        )
    )
    store.assign_verify_attempt(
        request_id=verify_request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-1",
        generation=1,
    )
    store.upsert_evaluator(
        evaluator_id="eval-1",
        worker_name="eval-1",
        expires_in_seconds=1,
    )

    dispatcher = EvaluatorDispatcher(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-2",
    )

    dispatcher.handle_dead_evaluators(now=time.time() + 60)

    build_record = store.load_build_request(build_request_id)
    verify_record = store.load_verify_request(verify_request_id)
    assert build_record is not None
    assert verify_record is not None
    assert build_record.state == "ready"
    assert build_record.generation == 2
    assert "attempt_id" not in build_record.payload
    assert "evaluator_id" not in build_record.payload
    assert not store.build_attempt_is_current(build_request_id, "attempt-build-1")
    assert verify_record.state == "blocked_on_build"
    assert verify_record.generation == 2
    assert "attempt_id" not in verify_record.payload
    assert "evaluator_id" not in verify_record.payload
    assert not store.verify_attempt_is_current(verify_request_id, "attempt-1")


def test_dead_evaluator_reblocks_ready_verify_requests_for_owned_lineage() -> None:
    from crsbench.distributed.evaluator_dispatcher import EvaluatorDispatcher

    redis_conn = _FakeRedis()
    store = DispatcherStateStore(redis_conn, experiment_name="exp-test")
    build_request_id = "build:trial-1:bench:0"
    verify_request_id = "verify:trial-2:bench:h1:pov-2"
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
    store.assign_build_attempt(
        request_id=build_request_id,
        evaluator_id="eval-1",
        attempt_id="attempt-build-1",
        generation=1,
    )
    store.submit_verify_request(
        VerifyRequestRecord(
            request_id=verify_request_id,
            trial_id="trial-2",
            benchmark="bench",
            harness="h1",
            pov_id="pov-2",
            owner_key="ownerB",
            lineage_id=lineage_id,
            generation=1,
            state="ready",
            build_request_ids=[build_request_id],
            payload={"trial_id": "trial-2"},
        )
    )
    store.upsert_evaluator(
        evaluator_id="eval-1",
        worker_name="eval-1",
        expires_in_seconds=1,
    )

    dispatcher = EvaluatorDispatcher(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-2",
    )

    dispatcher.handle_dead_evaluators(now=time.time() + 60)

    verify_record = store.load_verify_request(verify_request_id)
    assert verify_record is not None
    assert verify_record.state == "blocked_on_build"
    assert verify_record.generation == 2
