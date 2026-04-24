"""Tests for the evaluator global verify claim store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    from redis.exceptions import WatchError
except ImportError:  # pragma: no cover - redis is available in test env

    class WatchError(Exception):
        pass


from crsbench.distributed.evaluator_verify_claims import (
    EvaluatorVerifyClaimStore,
    VerifyClaim,
    VerifyRequestRecord,
)


class _FakePipeline:
    """Minimal optimistic-transaction pipeline for the fake Redis store."""

    def __init__(self, redis_conn: "_FakeRedis") -> None:
        self._redis = redis_conn
        self._watched_versions: dict[str, int] = {}
        self._commands: list[tuple[str, str, str, str]] = []
        self._in_multi = False

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.reset()

    def watch(self, *keys: str) -> None:
        self._watched_versions = {
            key: self._redis._versions.get(key, 0) for key in keys
        }

    def unwatch(self) -> None:
        self._watched_versions = {}

    def hget(self, key: str, field: str) -> str | None:
        return self._redis.hget(key, field)

    def hgetall(self, key: str) -> dict[str, str]:
        return self._redis.hgetall(key)

    def multi(self) -> None:
        self._in_multi = True

    def hset(self, key: str, field: str, value: str) -> None:
        if not self._in_multi:
            self._redis.hset(key, field, value)
            return
        self._commands.append(("hset", key, field, value))

    def execute(self) -> list[int]:
        for op, key, field, _value in self._commands:
            if op == "hset":
                self._redis._run_pre_write_hook(key, field)
        if any(
            self._redis._versions.get(key, 0) != version
            for key, version in self._watched_versions.items()
        ):
            self.reset()
            raise WatchError("watched key changed")
        results = []
        for op, key, field, value in self._commands:
            if op == "hset":
                results.append(self._redis._apply_hset(key, field, value))
        self.reset()
        return results

    def reset(self) -> None:
        self._watched_versions = {}
        self._commands = []
        self._in_multi = False


class _FakeRedis:
    """Minimal fake Redis hash storage for claim-store unit tests."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._versions: dict[str, int] = {}
        self._pre_write_hooks: dict[tuple[str, str], Callable[[], None]] = {}
        self._active_hooks: set[tuple[str, str]] = set()

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def set_pre_write_hook(
        self, key: str, field: str, callback: Callable[[], None]
    ) -> None:
        self._pre_write_hooks[(key, field)] = callback

    def _apply_hset(self, key: str, field: str, value: str) -> int:
        self._hashes.setdefault(key, {})[field] = value
        self._versions[key] = self._versions.get(key, 0) + 1
        return 1

    def _run_pre_write_hook(self, key: str, field: str) -> None:
        hook_key = (key, field)
        callback = self._pre_write_hooks.pop(hook_key, None)
        if callback is None or hook_key in self._active_hooks:
            return
        self._active_hooks.add(hook_key)
        try:
            callback()
        finally:
            self._active_hooks.remove(hook_key)

    def hset(self, key: str, field: str, value: str) -> int:
        self._run_pre_write_hook(key, field)
        return self._apply_hset(key, field, value)

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return self._hashes.get(key, {}).copy()

    def hdel(self, key: str, field: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket or field not in bucket:
            return 0
        del bucket[field]
        self._versions[key] = self._versions.get(key, 0) + 1
        if not bucket:
            self._hashes.pop(key, None)
        return 1


def test_claim_next_request_round_robins_owners() -> None:
    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store.submit_request(
        VerifyRequestRecord(
            request_id="verify:trial-1:bench-a:h1:pov-1",
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"benchmark": "bench-a"},
        )
    )
    store.submit_request(
        VerifyRequestRecord(
            request_id="verify:trial-2:bench-b:h1:pov-1",
            owner_key="trial::exp1::trial-2",
            request_kind="pov",
            payload={"benchmark": "bench-b"},
        )
    )

    first = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=30,
    )
    second = store.claim_next_request(
        evaluator_id="eval-2",
        now=101.0,
        lease_seconds=30,
    )

    assert first is not None
    assert first.request_id == "verify:trial-1:bench-a:h1:pov-1"
    assert first.claim == VerifyClaim(evaluator_id="eval-1", expires_at=130.0)

    assert second is not None
    assert second.request_id == "verify:trial-2:bench-b:h1:pov-1"
    assert second.claim == VerifyClaim(evaluator_id="eval-2", expires_at=131.0)


def test_expired_claim_can_be_reclaimed() -> None:
    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store.submit_request(
        VerifyRequestRecord(
            request_id="patch-verify:trial-1:bench:h1:cpv-1:patch-1",
            owner_key="trial::exp1::trial-1",
            request_kind="patch",
            payload={"benchmark": "bench"},
        )
    )

    first = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=5,
    )
    assert first is not None

    reclaimed = store.claim_next_request(
        evaluator_id="eval-2",
        now=106.0,
        lease_seconds=5,
    )

    assert reclaimed is not None
    assert reclaimed.request_id == "patch-verify:trial-1:bench:h1:cpv-1:patch-1"
    assert reclaimed.claim == VerifyClaim(evaluator_id="eval-2", expires_at=111.0)


def test_renew_claim_extends_active_lease() -> None:
    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store.submit_request(
        VerifyRequestRecord(
            request_id="verify:trial-1:bench:h1:pov-1",
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"benchmark": "bench"},
        )
    )

    claimed = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=5,
    )

    assert claimed is not None
    assert store.renew_claim(
        request_id="verify:trial-1:bench:h1:pov-1",
        evaluator_id="eval-1",
        now=103.0,
        lease_seconds=10,
    )

    renewed = store.load_request("verify:trial-1:bench:h1:pov-1")
    assert renewed is not None
    assert renewed.claim == VerifyClaim(evaluator_id="eval-1", expires_at=113.0)


def test_publish_result_requires_current_unexpired_claim() -> None:
    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:bench:h1:pov-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"benchmark": "bench"},
        )
    )
    claimed = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=5,
    )
    assert claimed is not None

    assert not store.publish_result_if_current(
        request_id=request_id,
        evaluator_id="eval-2",
        now=101.0,
        result={"status": "cpv"},
    )
    assert not store.publish_result_if_current(
        request_id=request_id,
        evaluator_id="eval-1",
        now=106.0,
        result={"status": "cpv"},
    )

    record = store.load_request(request_id)
    assert record is not None
    assert record.terminal_result is None

    reclaimed = store.claim_next_request(
        evaluator_id="eval-2",
        now=106.0,
        lease_seconds=10,
    )
    assert reclaimed is not None
    assert store.publish_result_if_current(
        request_id=request_id,
        evaluator_id="eval-2",
        now=107.0,
        result={"status": "cpv"},
    )

    published = store.load_request(request_id)
    assert published is not None
    assert published.claim is None
    assert published.terminal_result == {"status": "cpv"}


def test_claim_next_request_retries_when_concurrent_writer_wins() -> None:
    redis_conn = _FakeRedis()
    store1 = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store2 = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request = VerifyRequestRecord(
        request_id="verify:trial-1:bench-a:h1:pov-1",
        owner_key="trial::exp1::trial-1",
        request_kind="pov",
        payload={"benchmark": "bench-a"},
    )
    store1.submit_request(request)
    redis_conn.set_pre_write_hook(
        store1._requests_key(),
        request.request_id,
        lambda: store2.claim_next_request(
            evaluator_id="eval-2",
            now=100.0,
            lease_seconds=30,
        ),
    )

    claimed = store1.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=30,
    )

    assert claimed is None
    stored = store1.load_request(request.request_id)
    assert stored is not None
    assert stored.claim == VerifyClaim(evaluator_id="eval-2", expires_at=130.0)


def test_publish_result_if_current_retries_when_claim_changes_before_write() -> None:
    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:bench:h1:pov-1"
    request = VerifyRequestRecord(
        request_id=request_id,
        owner_key="trial::exp1::trial-1",
        request_kind="pov",
        payload={"benchmark": "bench"},
    )
    store.submit_request(request)
    claimed = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=10,
    )
    assert claimed is not None
    redis_conn.set_pre_write_hook(
        store._requests_key(),
        request_id,
        lambda: store.submit_request(
            VerifyRequestRecord(
                request_id=request_id,
                owner_key=request.owner_key,
                request_kind=request.request_kind,
                payload=dict(request.payload),
                claim=VerifyClaim(evaluator_id="eval-2", expires_at=200.0),
                terminal_result=None,
            )
        ),
    )

    assert not store.publish_result_if_current(
        request_id=request_id,
        evaluator_id="eval-1",
        now=105.0,
        result={"status": "cpv"},
    )

    stored = store.load_request(request_id)
    assert stored is not None
    assert stored.claim == VerifyClaim(evaluator_id="eval-2", expires_at=200.0)
    assert stored.terminal_result is None
