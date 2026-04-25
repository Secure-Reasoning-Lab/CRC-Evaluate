"""Tests for evaluator-side claim-loop DAG materialization."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.distributed.evaluator_jobs import EmbeddedPov, SinglePovPayload
from crsbench.distributed.evaluator_verify_claims import (
    EvaluatorVerifyClaimStore,
    VerifyRequestRecord,
)
from crsbench.distributed.patch_evaluator_jobs import EmbeddedPatch, PatchJobPayload


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


class _FakeJob:
    def __init__(
        self,
        job_id: str,
        *,
        status: str = "queued",
        result: object | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        self.id = job_id
        self._status = status
        self.result = result
        self.meta = dict(meta or {})
        self.save_meta_calls = 0

    def get_status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        self._status = status

    def save_meta(self) -> None:
        self.save_meta_calls += 1


class _FakeQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self.jobs: dict[str, _FakeJob] = {}
        self.enqueued: list[dict[str, object]] = []

    def fetch_job(self, job_id: str) -> _FakeJob | None:
        return self.jobs.get(job_id)

    def enqueue(
        self,
        func_name: str,
        payload: dict[str, object],
        *,
        job_timeout: int,
        result_ttl: int,
        job_id: str,
        depends_on: list[object] | None = None,
        meta: dict[str, object] | None = None,
    ) -> _FakeJob:
        job = self.jobs.get(job_id)
        if job is None:
            job = _FakeJob(job_id)
            self.jobs[job_id] = job
        self.enqueued.append(
            {
                "func_name": func_name,
                "payload": payload,
                "job_timeout": job_timeout,
                "result_ttl": result_ttl,
                "job_id": job_id,
                "depends_on": list(depends_on or []),
                "meta": dict(meta or {}),
            }
        )
        return job


def _make_engine_and_adapter():
    builder_infra = MagicMock(
        inc_image_policy="auto",
        inc_image_registry="ghcr.io/example",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=45,
        local_image_prefix="crsbench",
    )
    builder = MagicMock()
    builder.source_mode = "main_repo"
    builder.infra = builder_infra

    adapter = MagicMock()
    adapter.benchmark_path = Path("/benchmarks/test-benchmark")
    adapter.benchmark_name = "test-benchmark"
    adapter.main_repo = "https://example.com/repo.git"
    adapter.get_mode.return_value = BenchmarkMode.DELTA
    adapter.get_base_commit.return_value = "a" * 40
    adapter.get_ref_commit.return_value = "b" * 40
    adapter.get_cpv_numbers.return_value = [0]
    adapter.lang = "c"
    adapter.repo_name = "repo"
    adapter.get_all_cpv_sanitizers.return_value = ["address"]
    adapter.inc_build = True

    config_a = MagicMock()
    config_a.benchmark_path = Path("/benchmarks/test-benchmark")
    config_a.benchmark_name = "test-benchmark"
    config_a.variant_type = VariantType.DELTA_REF
    config_a.commit = "b" * 40
    config_a.main_repo = "https://example.com/repo.git"
    config_a.mode = BenchmarkMode.DELTA
    config_a.language = "c"
    config_a.cpv_num = None
    config_a.patch_id = None
    config_a.pov_id = None
    config_a.patches = []
    config_a.use_inc_build = True
    config_a.sanitizer = "address"
    config_a.repo_name = "repo"
    config_a.variant_name = "test-benchmark-asan-deltaref"

    config_b = MagicMock()
    config_b.benchmark_path = Path("/benchmarks/test-benchmark")
    config_b.benchmark_name = "test-benchmark"
    config_b.variant_type = VariantType.CPV
    config_b.commit = "b" * 40
    config_b.main_repo = "https://example.com/repo.git"
    config_b.mode = BenchmarkMode.DELTA
    config_b.language = "c"
    config_b.cpv_num = 0
    config_b.patch_id = None
    config_b.pov_id = None
    config_b.patches = []
    config_b.use_inc_build = True
    config_b.sanitizer = "address"
    config_b.repo_name = "repo"
    config_b.variant_name = "test-benchmark-asan-delta-cpv0"

    builder.create_build_plan.return_value = MagicMock(configs=[config_a, config_b])
    engine = MagicMock()
    engine.builder = builder
    engine.load_adapter.return_value = adapter
    return engine, adapter


def test_claim_worker_materializes_pov_with_local_build_ids_and_unfinished_deps() -> (
    None
):
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        build_local_ci_job_id,
        build_local_verify_job_id,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    payload = SinglePovPayload(
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="test-benchmark",
        harness="h1",
        pov=EmbeddedPov.from_bytes("pov-1", b"boom"),
        enqueued_at=100.0,
        sanitizer="address",
        build_job_ids=[
            "build-single/test-benchmark/test-benchmark-asan-deltaref",
            "build-single/test-benchmark/test-benchmark-asan-delta-cpv0",
        ],
        build_artifact_ids=[
            "build-single/test-benchmark/test-benchmark-asan-deltaref",
            "build-single/test-benchmark/test-benchmark-asan-delta-cpv0",
        ],
        source_mode="main_repo",
        use_inc_build=True,
    )
    store.submit_request(
        VerifyRequestRecord(
            request_id="verify:trial-1:test-benchmark:h1:pov-1",
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload=payload.to_dict(),
        )
    )

    engine, _adapter = _make_engine_and_adapter()
    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    finished_build_id = build_local_ci_job_id(
        "build-single/test-benchmark/test-benchmark-asan-deltaref/main_repo/inc",
        evaluator_id="eval-1",
    )
    build_queue.jobs[finished_build_id] = _FakeJob(
        finished_build_id,
        status="finished",
        result={"success": True},
    )

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=engine,
        benchmarks_root=Path("/benchmarks"),
    )

    claimed = worker.dispatch_one(now=100.0)

    assert claimed is not None
    assert [entry["job_id"] for entry in build_queue.enqueued] == [
        "prepare-inc-image/test-benchmark/address/main_repo/inc/cached/local/eval-1",
        "build-single/test-benchmark/test-benchmark-asan-delta-cpv0/main_repo/inc/local/eval-1",
    ]
    assert len(verify_queue.enqueued) == 1
    verify_entry = verify_queue.enqueued[0]
    assert verify_entry["func_name"] == (
        "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify"
    )
    assert verify_entry["job_id"] == build_local_verify_job_id(
        request_id="verify:trial-1:test-benchmark:h1:pov-1",
        evaluator_id="eval-1",
    )
    assert [dep.id for dep in verify_entry["depends_on"]] == [
        "build-single/test-benchmark/test-benchmark-asan-delta-cpv0/main_repo/inc/local/eval-1"
    ]
    claimed_payload = verify_entry["payload"]
    assert claimed_payload["request_id"] == "verify:trial-1:test-benchmark:h1:pov-1"
    assert claimed_payload["request_kind"] == "pov"
    verify_payload = claimed_payload["verify_payload"]
    assert verify_payload["build_job_ids"] == [
        finished_build_id,
        "build-single/test-benchmark/test-benchmark-asan-delta-cpv0/main_repo/inc/local/eval-1",
    ]
    assert verify_payload["build_artifact_ids"] == verify_payload["build_job_ids"]
    assert worker.has_pending_required_builds()


def test_claim_worker_materializes_patch_with_local_build_dependency() -> None:
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        build_local_ci_job_id,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    patch_payload = PatchJobPayload(
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="test-benchmark",
        harness="h1",
        cpv_id="cpv-1",
        patch=EmbeddedPatch(
            patch_id="patch-1",
            pov_id="cpv-1",
            patch_content_b64="cGF0Y2g=",
        ),
        sanitizer="address",
        source_mode="main_repo",
        verify_variants=True,
        test_mode="FULL",
        use_inc_build=True,
        enqueued_at=100.0,
    )
    request_id = "patch-verify:trial-1:test-benchmark:h1:cpv-1:patch-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="patch",
            payload=patch_payload.to_dict(),
        )
    )

    engine = MagicMock()
    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=engine,
        benchmarks_root=Path("/benchmarks"),
    )

    claimed = worker.dispatch_one(now=100.0)

    assert claimed is not None
    assert len(build_queue.enqueued) == 1
    build_entry = build_queue.enqueued[0]
    assert build_entry["func_name"] == (
        "crsbench.distributed.patch_evaluator_jobs.execute_patch_build"
    )
    local_build_id = build_local_ci_job_id(
        build_entry["job_id"],
        evaluator_id="eval-1",
    )
    assert len(verify_queue.enqueued) == 1
    verify_entry = verify_queue.enqueued[0]
    assert verify_entry["func_name"] == (
        "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify"
    )
    assert [dep.id for dep in verify_entry["depends_on"]] == [build_entry["job_id"]]
    claimed_payload = verify_entry["payload"]
    assert claimed_payload["request_id"] == request_id
    assert claimed_payload["request_kind"] == "patch"
    assert (
        claimed_payload["verify_payload"]["build_patch_job_id"] == build_entry["job_id"]
    )


def test_claim_worker_releases_claim_when_materialization_fails() -> None:
    from crsbench.distributed.evaluator_claim_worker import EvaluatorClaimWorker

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:test-benchmark:h1:pov-1"
    payload = SinglePovPayload(
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="test-benchmark",
        harness="h1",
        pov=EmbeddedPov.from_bytes("pov-1", b"boom"),
        enqueued_at=100.0,
        sanitizer="address",
        build_job_ids=[],
        build_artifact_ids=[],
        source_mode="main_repo",
        use_inc_build=True,
    )
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload=payload.to_dict(),
        )
    )

    engine = MagicMock()
    engine.builder = MagicMock()
    engine.builder.source_mode = "main_repo"
    engine.load_adapter.return_value = None
    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=engine,
        benchmarks_root=Path("/benchmarks"),
    )

    claimed = worker.dispatch_one(now=100.0)

    assert claimed is None
    stored = store.load_request(request_id)
    assert stored is not None
    assert stored.claim is None
    assert stored.terminal_result is None
    assert build_queue.enqueued == []
    assert verify_queue.enqueued == []
    assert not worker.has_pending_required_builds()

    reclaimed = store.claim_next_request(
        evaluator_id="eval-2",
        now=100.0,
        lease_seconds=30,
    )
    assert reclaimed is not None
    assert reclaimed.claim is not None
    assert reclaimed.claim.evaluator_id == "eval-2"


def test_refresh_active_claims_releases_claim_after_local_verify_failure() -> None:
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        _ActiveClaim,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:test-benchmark:h1:pov-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload={"benchmark": "test-benchmark"},
        )
    )
    claimed = store.claim_next_request(
        evaluator_id="eval-1",
        now=100.0,
        lease_seconds=30,
    )
    assert claimed is not None

    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    failed_verify_job = _FakeJob("claim-verify/eval-1/test", status="failed")
    verify_queue.jobs[failed_verify_job.id] = failed_verify_job

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
    )
    worker._active_claims[request_id] = _ActiveClaim(
        local_verify_job_id=failed_verify_job.id,
        required_build_job_ids=(),
    )

    worker.refresh_active_claims(now=105.0)

    assert request_id not in worker._active_claims
    record = store.load_request(request_id)
    assert record is not None
    assert record.claim is None
    assert record.terminal_result is None


def test_has_pending_required_builds_tolerates_concurrent_active_claim_refresh() -> (
    None
):
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        _ActiveClaim,
    )

    class _BlockingBuildQueue(_FakeQueue):
        def __init__(self) -> None:
            super().__init__("build-q")
            self.fetch_started = threading.Event()
            self.allow_fetch = threading.Event()
            self._calls = 0

        def fetch_job(self, job_id: str) -> _FakeJob | None:
            self._calls += 1
            if self._calls == 1:
                self.fetch_started.set()
                assert self.allow_fetch.wait(timeout=5)
            return _FakeJob(job_id, status="finished")

    redis_conn = _FakeRedis()
    build_queue = _BlockingBuildQueue()
    verify_queue = _FakeQueue("verify-q")
    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
    )
    worker._active_claims["request-1"] = _ActiveClaim(
        local_verify_job_id="verify-1",
        required_build_job_ids=("build-1",),
    )
    worker._active_claims["request-2"] = _ActiveClaim(
        local_verify_job_id="verify-2",
        required_build_job_ids=("build-2",),
    )

    result: list[bool] = []
    errors: list[BaseException] = []

    def _check_pending() -> None:
        try:
            result.append(worker.has_pending_required_builds())
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            errors.append(exc)

    thread = threading.Thread(target=_check_pending)
    thread.start()

    assert build_queue.fetch_started.wait(timeout=5)

    worker.refresh_active_claims(now=105.0)
    build_queue.allow_fetch.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert result == [False]


def test_has_pending_required_builds_returns_true_while_claim_materialization_is_in_progress() -> (
    None
):
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        _ActiveClaim,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store.submit_request(
        VerifyRequestRecord(
            request_id="request-1",
            owner_key="trial::exp1::request-1",
            request_kind="pov",
            payload={"benchmark": "test-benchmark"},
        )
    )

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=_FakeQueue("build-q"),
        verify_queue=_FakeQueue("verify-q"),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )

    materialize_started = threading.Event()
    allow_materialize = threading.Event()

    def _materialize(record: VerifyRequestRecord) -> _ActiveClaim:
        materialize_started.set()
        assert allow_materialize.wait(timeout=5)
        return _ActiveClaim(
            local_verify_job_id=f"verify-{record.request_id}",
            required_build_job_ids=(),
        )

    worker._materialize_claimed_request = _materialize  # type: ignore[method-assign]

    results: list[VerifyRequestRecord | None] = []
    errors: list[BaseException] = []

    def _dispatch() -> None:
        try:
            results.append(worker.dispatch_one(now=time.time()))
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            errors.append(exc)

    thread = threading.Thread(target=_dispatch)
    thread.start()

    assert materialize_started.wait(timeout=5)
    try:
        assert worker.has_pending_required_builds() is True
    finally:
        allow_materialize.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0] is not None


def test_has_pending_required_builds_stays_false_during_empty_claim_poll() -> None:
    from crsbench.distributed.evaluator_claim_worker import EvaluatorClaimWorker

    worker = EvaluatorClaimWorker(
        redis_conn=_FakeRedis(),
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=_FakeQueue("build-q"),
        verify_queue=_FakeQueue("verify-q"),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )

    claim_started = threading.Event()
    allow_claim = threading.Event()

    def _claim_next_request(*, evaluator_id: str, now: float, lease_seconds: int):
        del evaluator_id, now, lease_seconds
        claim_started.set()
        assert allow_claim.wait(timeout=5)

    worker.store.claim_next_request = _claim_next_request  # type: ignore[method-assign]

    results: list[VerifyRequestRecord | None] = []
    errors: list[BaseException] = []

    def _dispatch() -> None:
        try:
            results.append(worker.dispatch_one(now=100.0))
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            errors.append(exc)

    thread = threading.Thread(target=_dispatch)
    thread.start()

    assert claim_started.wait(timeout=5)
    try:
        assert worker.has_pending_required_builds() is False
    finally:
        allow_claim.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert results == [None]


def test_has_pending_required_builds_ignores_expired_claims() -> None:
    from crsbench.distributed.evaluator_claim_worker import EvaluatorClaimWorker
    from crsbench.distributed.evaluator_verify_claims import VerifyClaim

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    store.submit_request(
        VerifyRequestRecord(
            request_id="request-1",
            owner_key="trial::exp1::request-1",
            request_kind="pov",
            payload={"benchmark": "test-benchmark"},
            claim=VerifyClaim(evaluator_id="eval-1", expires_at=time.time() - 1.0),
        )
    )

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=_FakeQueue("build-q"),
        verify_queue=_FakeQueue("verify-q"),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )

    assert worker.has_pending_required_builds() is False


@pytest.mark.parametrize(
    "terminal_status",
    ["failed", "finished", "stopped", "canceled", "cancelled"],
)
def test_reclaimed_request_reenqueues_fresh_local_verify_job(
    terminal_status: str,
) -> None:
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        build_local_verify_job_id,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    patch_payload = PatchJobPayload(
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="test-benchmark",
        harness="h1",
        cpv_id="cpv-1",
        patch=EmbeddedPatch(
            patch_id="patch-1",
            pov_id="cpv-1",
            patch_content_b64="cGF0Y2g=",
        ),
        sanitizer="address",
        source_mode="main_repo",
        verify_variants=True,
        test_mode="FULL",
        use_inc_build=True,
        enqueued_at=100.0,
    )
    request_id = "patch-verify:trial-1:test-benchmark:h1:cpv-1:patch-1"
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="patch",
            payload=patch_payload.to_dict(),
        )
    )

    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
    )

    claimed = worker.dispatch_one(now=100.0)
    assert claimed is not None

    first_build_job_id = build_queue.enqueued[0]["job_id"]
    first_build_job = build_queue.jobs[first_build_job_id]

    local_verify_job_id = build_local_verify_job_id(
        request_id=request_id,
        evaluator_id="eval-1",
    )
    first_verify_job = verify_queue.jobs[local_verify_job_id]
    first_verify_job.set_status(terminal_status)

    worker.refresh_active_claims(now=101.0)
    assert request_id not in worker._active_claims

    released = store.load_request(request_id)
    assert released is not None
    assert released.claim is None
    assert released.terminal_result is None

    reclaimed = worker.dispatch_one(now=102.0)
    assert reclaimed is not None
    assert reclaimed.request_id == request_id
    assert request_id in worker._active_claims
    assert len(build_queue.enqueued) == 1
    assert build_queue.jobs[first_build_job_id] is first_build_job
    assert len(verify_queue.enqueued) == 2

    second_verify_job = verify_queue.jobs[local_verify_job_id]
    assert second_verify_job is not first_verify_job
    assert second_verify_job.get_status() == "queued"

    worker.refresh_active_claims(now=103.0)

    active = store.load_request(request_id)
    assert active is not None
    assert active.claim is not None
    assert active.claim.evaluator_id == "eval-1"
    assert active.terminal_result is None


def test_reclaimed_pov_request_reenqueues_fresh_local_verify_job() -> None:
    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        build_local_ci_job_id,
        build_local_verify_job_id,
    )

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    request_id = "verify:trial-1:test-benchmark:h1:pov-1"
    payload = SinglePovPayload(
        experiment_name="exp1",
        trial_id="trial-1",
        benchmark="test-benchmark",
        harness="h1",
        pov=EmbeddedPov.from_bytes("pov-1", b"boom"),
        enqueued_at=100.0,
        sanitizer="address",
        build_job_ids=[
            "build-single/test-benchmark/test-benchmark-asan-deltaref",
            "build-single/test-benchmark/test-benchmark-asan-delta-cpv0",
        ],
        build_artifact_ids=[
            "build-single/test-benchmark/test-benchmark-asan-deltaref",
            "build-single/test-benchmark/test-benchmark-asan-delta-cpv0",
        ],
        source_mode="main_repo",
        use_inc_build=True,
    )
    store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp1::trial-1",
            request_kind="pov",
            payload=payload.to_dict(),
        )
    )

    engine, _adapter = _make_engine_and_adapter()
    build_queue = _FakeQueue("build-q")
    verify_queue = _FakeQueue("verify-q")
    finished_build_id = build_local_ci_job_id(
        "build-single/test-benchmark/test-benchmark-asan-deltaref/main_repo/inc",
        evaluator_id="eval-1",
    )
    build_queue.jobs[finished_build_id] = _FakeJob(
        finished_build_id,
        status="finished",
        result={"success": True},
    )

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=build_queue,
        verify_queue=verify_queue,
        verification_engine=engine,
        benchmarks_root=Path("/benchmarks"),
    )

    claimed = worker.dispatch_one(now=100.0)
    assert claimed is not None

    first_enqueued_build_job_ids = [entry["job_id"] for entry in build_queue.enqueued]
    first_enqueued_build_jobs = {
        job_id: build_queue.jobs[job_id] for job_id in first_enqueued_build_job_ids
    }
    local_verify_job_id = build_local_verify_job_id(
        request_id=request_id,
        evaluator_id="eval-1",
    )
    first_verify_job = verify_queue.jobs[local_verify_job_id]
    first_verify_job.set_status("finished")

    worker.refresh_active_claims(now=101.0)
    assert request_id not in worker._active_claims

    released = store.load_request(request_id)
    assert released is not None
    assert released.claim is None
    assert released.terminal_result is None

    reclaimed = worker.dispatch_one(now=102.0)
    assert reclaimed is not None
    assert reclaimed.request_id == request_id
    assert request_id in worker._active_claims
    assert len(build_queue.enqueued) == len(first_enqueued_build_job_ids)
    for job_id, job in first_enqueued_build_jobs.items():
        assert build_queue.jobs[job_id] is job
    assert len(verify_queue.enqueued) == 2

    second_verify_job = verify_queue.jobs[local_verify_job_id]
    assert second_verify_job is not first_verify_job
    assert second_verify_job.get_status() == "queued"


def test_tick_claims_until_inflight_limit() -> None:
    from crsbench.distributed.evaluator_claim_worker import EvaluatorClaimWorker

    redis_conn = _FakeRedis()
    store = EvaluatorVerifyClaimStore(redis_conn, experiment_name="exp1")
    for index in (1, 2):
        patch_payload = PatchJobPayload(
            experiment_name="exp1",
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="h1",
            cpv_id=f"cpv-{index}",
            patch=EmbeddedPatch(
                patch_id=f"patch-{index}",
                pov_id=f"cpv-{index}",
                patch_content_b64="cGF0Y2g=",
            ),
            sanitizer="address",
            source_mode="main_repo",
            verify_variants=True,
            test_mode="FULL",
            use_inc_build=True,
            enqueued_at=100.0,
        )
        store.submit_request(
            VerifyRequestRecord(
                request_id=f"patch-verify:trial-1:test-benchmark:h1:cpv-{index}:patch-{index}",
                owner_key=f"trial::exp1::trial-{index}",
                request_kind="patch",
                payload=patch_payload.to_dict(),
            )
        )

    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=_FakeQueue("build-q"),
        verify_queue=_FakeQueue("verify-q"),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=2,
    )

    claimed = worker.tick(now=100.0)

    assert claimed is not None
    assert len(worker._active_claims) == 2
    assert len(worker.build_queue.enqueued) == 2
    assert len(worker.verify_queue.enqueued) == 2


def test_enqueue_or_reuse_job_adopts_trial_owner_for_reused_warmup_job() -> None:
    from crsbench.distributed.evaluator_claim_worker import _enqueue_or_reuse_job
    from crsbench.distributed.evaluator_scheduler import SCHEDULER_OWNER_KEY_META

    queue = _FakeQueue("build-q")
    existing = _FakeJob(
        "build-single/test-benchmark/test-benchmark-asan-deltaref/main_repo/inc/local/eval-1",
        meta={SCHEDULER_OWNER_KEY_META: "unit::exp1::test-benchmark::address::build"},
    )
    queue.jobs[existing.id] = existing

    reused = _enqueue_or_reuse_job(
        queue,
        "crsbench.distributed.build_jobs.execute_ci_build",
        {"benchmark_name": "test-benchmark"},
        job_timeout=3600,
        job_id=existing.id,
        meta={
            "experiment_name": "exp1",
            SCHEDULER_OWNER_KEY_META: "trial::exp1::trial-1",
        },
    )

    assert reused is existing
    assert existing.meta[SCHEDULER_OWNER_KEY_META] == "trial::exp1::trial-1"
    assert existing.save_meta_calls == 1


def test_enqueue_or_reuse_job_refreshes_terminal_verify_job_via_queue_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crsbench.distributed.queue as queue_module
    from crsbench.distributed.evaluator_claim_worker import _enqueue_or_reuse_job

    queue = _FakeQueue("verify-q")
    terminal_job = _FakeJob(
        "claim-verify/eval-1/request-1",
        status="finished",
    )
    queue.jobs[terminal_job.id] = terminal_job

    removed_job_ids: list[str] = []

    def remove_job(queue_obj: _FakeQueue, job_id: str) -> bool:
        removed_job_ids.append(job_id)
        queue_obj.jobs.pop(job_id, None)
        return True

    monkeypatch.setattr(queue_module, "remove_job_by_id", remove_job)

    refreshed = _enqueue_or_reuse_job(
        queue,
        "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
        {"request_id": "verify:trial-1:test-benchmark:h1:pov-1"},
        job_timeout=3600,
        job_id=terminal_job.id,
        meta={"experiment_name": "exp1"},
        refresh_terminal=True,
    )

    assert removed_job_ids == [terminal_job.id]
    assert len(queue.enqueued) == 1
    assert refreshed.id == terminal_job.id
    assert refreshed is not terminal_job
    assert refreshed.get_status() == "queued"


def test_enqueue_or_reuse_job_refreshes_terminal_verify_job_after_duplicate_id_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crsbench.distributed.queue as queue_module
    from crsbench.distributed.evaluator_claim_worker import _enqueue_or_reuse_job

    class _RacingQueue(_FakeQueue):
        def __init__(self, name: str, *, raced_job: _FakeJob) -> None:
            super().__init__(name)
            self._raced_job = raced_job
            self.enqueue_attempts = 0

        def enqueue(
            self,
            func_name: str,
            payload: dict[str, object],
            *,
            job_timeout: int,
            result_ttl: int,
            job_id: str,
            depends_on: list[object] | None = None,
            meta: dict[str, object] | None = None,
        ) -> _FakeJob:
            self.enqueue_attempts += 1
            if self.enqueue_attempts == 1:
                self.jobs[job_id] = self._raced_job
                raise RuntimeError(f"job id {job_id} already exists")
            return super().enqueue(
                func_name,
                payload,
                job_timeout=job_timeout,
                result_ttl=result_ttl,
                job_id=job_id,
                depends_on=depends_on,
                meta=meta,
            )

    raced_job = _FakeJob(
        "claim-verify/eval-1/request-2",
        status="finished",
    )
    queue = _RacingQueue("verify-q", raced_job=raced_job)
    removed_job_ids: list[str] = []

    def remove_job(queue_obj: _FakeQueue, job_id: str) -> bool:
        removed_job_ids.append(job_id)
        queue_obj.jobs.pop(job_id, None)
        return True

    monkeypatch.setattr(queue_module, "remove_job_by_id", remove_job)

    refreshed = _enqueue_or_reuse_job(
        queue,
        "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
        {"request_id": "verify:trial-1:test-benchmark:h1:pov-2"},
        job_timeout=3600,
        job_id=raced_job.id,
        meta={"experiment_name": "exp1"},
        refresh_terminal=True,
    )

    assert queue.enqueue_attempts == 2
    assert removed_job_ids == [raced_job.id]
    assert len(queue.enqueued) == 1
    assert refreshed.id == raced_job.id
    assert refreshed is not raced_job
    assert refreshed.get_status() == "queued"


def test_enqueue_or_reuse_job_raises_when_terminal_verify_job_persists_after_reported_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crsbench.distributed.queue as queue_module
    from crsbench.distributed.evaluator_claim_worker import _enqueue_or_reuse_job

    class _OpaqueQueue:
        def __init__(self, name: str) -> None:
            self.name = name
            self._jobs: dict[str, _FakeJob] = {}
            self.enqueued: list[dict[str, object]] = []

        def fetch_job(self, job_id: str) -> _FakeJob | None:
            return self._jobs.get(job_id)

        def enqueue(
            self,
            func_name: str,
            payload: dict[str, object],
            *,
            job_timeout: int,
            result_ttl: int,
            job_id: str,
            depends_on: list[object] | None = None,
            meta: dict[str, object] | None = None,
        ) -> _FakeJob:
            job = _FakeJob(job_id)
            self._jobs[job_id] = job
            self.enqueued.append(
                {
                    "func_name": func_name,
                    "payload": payload,
                    "job_timeout": job_timeout,
                    "result_ttl": result_ttl,
                    "job_id": job_id,
                    "depends_on": list(depends_on or []),
                    "meta": dict(meta or {}),
                }
            )
            return job

    queue = _OpaqueQueue("verify-q")
    terminal_job = _FakeJob(
        "claim-verify/eval-1/request-stale",
        status="finished",
    )
    queue._jobs[terminal_job.id] = terminal_job

    removed_job_ids: list[str] = []

    def remove_job(queue_obj: _OpaqueQueue, job_id: str) -> bool:
        del queue_obj
        removed_job_ids.append(job_id)
        return True

    monkeypatch.setattr(queue_module, "remove_job_by_id", remove_job)

    with pytest.raises(RuntimeError, match="Failed to remove terminal job"):
        _enqueue_or_reuse_job(
            queue,
            "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
            {"request_id": "verify:trial-1:test-benchmark:h1:pov-stale"},
            job_timeout=3600,
            job_id=terminal_job.id,
            meta={"experiment_name": "exp1"},
            refresh_terminal=True,
        )

    assert removed_job_ids == [terminal_job.id]
    assert queue._jobs[terminal_job.id] is terminal_job
    assert queue.enqueued == []


def test_enqueue_or_reuse_job_raises_after_duplicate_id_race_when_terminal_job_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crsbench.distributed.queue as queue_module
    from crsbench.distributed.evaluator_claim_worker import _enqueue_or_reuse_job

    class _RacingQueue:
        def __init__(self, name: str, *, raced_job: _FakeJob) -> None:
            self.name = name
            self._raced_job = raced_job
            self.enqueue_attempts = 0
            self._jobs: dict[str, _FakeJob] = {}
            self.enqueued: list[dict[str, object]] = []

        def fetch_job(self, job_id: str) -> _FakeJob | None:
            return self._jobs.get(job_id)

        def enqueue(
            self,
            func_name: str,
            payload: dict[str, object],
            *,
            job_timeout: int,
            result_ttl: int,
            job_id: str,
            depends_on: list[object] | None = None,
            meta: dict[str, object] | None = None,
        ) -> _FakeJob:
            self.enqueue_attempts += 1
            if self.enqueue_attempts == 1:
                self._jobs[job_id] = self._raced_job
                raise RuntimeError(f"job id {job_id} already exists")
            job = _FakeJob(job_id)
            self._jobs[job_id] = job
            self.enqueued.append(
                {
                    "func_name": func_name,
                    "payload": payload,
                    "job_timeout": job_timeout,
                    "result_ttl": result_ttl,
                    "job_id": job_id,
                    "depends_on": list(depends_on or []),
                    "meta": dict(meta or {}),
                }
            )
            return job

    raced_job = _FakeJob(
        "claim-verify/eval-1/request-stale-race",
        status="finished",
    )
    queue = _RacingQueue("verify-q", raced_job=raced_job)
    removed_job_ids: list[str] = []

    def remove_job(queue_obj: _RacingQueue, job_id: str) -> bool:
        del queue_obj
        removed_job_ids.append(job_id)
        return True

    monkeypatch.setattr(queue_module, "remove_job_by_id", remove_job)

    with pytest.raises(RuntimeError, match="Failed to remove terminal job"):
        _enqueue_or_reuse_job(
            queue,
            "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
            {"request_id": "verify:trial-1:test-benchmark:h1:pov-stale-race"},
            job_timeout=3600,
            job_id=raced_job.id,
            meta={"experiment_name": "exp1"},
            refresh_terminal=True,
        )

    assert queue.enqueue_attempts == 1
    assert removed_job_ids == [raced_job.id]
    assert queue._jobs[raced_job.id] is raced_job
    assert queue.enqueued == []
