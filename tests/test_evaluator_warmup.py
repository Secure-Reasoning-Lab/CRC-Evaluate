"""Tests for dispatcher-local evaluator warmup feeder."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    build_scheduler_owner_key_for_ci_job,
)
from crsbench.distributed.evaluator_warmup import (
    DispatcherWarmupFeeder,
    WarmupBuildSpec,
    build_dispatcher_warmup_specs,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeRegistry:
    def __init__(self, job_ids: list[str]) -> None:
        self._job_ids = list(job_ids)

    def get_job_ids(self) -> list[str]:
        return list(self._job_ids)


class _FakeIntermediateQueue:
    def __init__(self, job_ids: list[str]) -> None:
        self._job_ids = list(job_ids)

    def get_job_ids(self) -> list[str]:
        return list(self._job_ids)


class _FakeQueue:
    def __init__(
        self,
        *,
        queued_job_ids: list[str] | None = None,
        intermediate_job_ids: list[str] | None = None,
        started_job_ids: list[str] | None = None,
    ) -> None:
        self._queued_job_ids = list(queued_job_ids or [])
        self.intermediate_queue = _FakeIntermediateQueue(intermediate_job_ids or [])
        self.started_job_registry = _FakeRegistry(started_job_ids or [])
        self.enqueued: list[dict[str, Any]] = []

    def get_job_ids(self) -> list[str]:
        return list(self._queued_job_ids)

    def enqueue(
        self,
        func_name: str,
        payload: dict[str, Any],
        *,
        job_timeout: int,
        result_ttl: int,
        job_id: str,
        meta: dict[str, Any],
    ) -> None:
        self.enqueued.append(
            {
                "func_name": func_name,
                "payload": payload,
                "job_timeout": job_timeout,
                "result_ttl": result_ttl,
                "job_id": job_id,
                "meta": meta,
            }
        )


class _FakeStateStore:
    def __init__(self, *, pending_required_builds: bool) -> None:
        self._pending_required_builds = pending_required_builds

    def has_pending_required_builds(self) -> bool:
        return self._pending_required_builds

    def set_pending_required_builds(self, value: bool) -> None:
        self._pending_required_builds = value


def _warmup_specs(count: int) -> list[WarmupBuildSpec]:
    return [
        WarmupBuildSpec(
            job_id=f"warmup-build-{index}",
            payload={"id": index},
            meta={"experiment_name": "exp-test", "warmup": "true"},
        )
        for index in range(count)
    ]


@patch("crsbench.distributed.ci_jobs.serialize_ci_job")
@patch("crsbench.executor.variant_planner.VariantPlanner")
@patch("crsbench.distributed.evaluator_warmup.filter_benchmarks_by_mode")
def test_build_dispatcher_warmup_specs_plans_serialized_build_jobs(
    mock_filter: MagicMock,
    mock_planner_cls: MagicMock,
    mock_serialize: MagicMock,
    tmp_path: Path,
) -> None:
    benchmark_name_a = "afc-mock-full-01"
    benchmark_name_b = "afc-mock-full-02"
    benchmark_path_a = tmp_path / benchmark_name_a
    benchmark_path_b = tmp_path / benchmark_name_b
    benchmark_path_a.mkdir(parents=True, exist_ok=True)
    benchmark_path_b.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(
        benchmarks_root=tmp_path,
        mode=SimpleNamespace(value="auto"),
        source_mode="pkgs",
        get_benchmark_list=lambda: [benchmark_name_a, benchmark_name_b],
    )
    planner = MagicMock()
    job_a = MagicMock()
    job_a.job_id = "build-single/bench/variant-a/pkgs/inc"
    job_a.benchmark_name = "bench-a"
    job_a.benchmark = "bench-a"
    job_a.harness = "harness-a"
    job_a.cpv_id = None
    job_a.sanitizer = "address"
    job_a.job_type = "build"
    job_b = MagicMock()
    job_b.job_id = "build-single/bench/variant-b/pkgs/inc"
    job_b.benchmark_name = "bench-b"
    job_b.benchmark = "bench-b"
    job_b.harness = "harness-b"
    job_b.cpv_id = None
    job_b.sanitizer = "address"
    job_b.job_type = "build"
    planner.iter_builds.side_effect = [iter([job_a]), iter([job_b])]
    mock_planner_cls.return_value = planner
    mock_filter.side_effect = lambda names, _mode, _root: names
    mock_serialize.side_effect = [{"kind": "build-a"}, {"kind": "build-b"}]

    specs = build_dispatcher_warmup_specs(
        config,
        experiment_name="exp-test",
        evaluator_id="eval-1",
        oss_fuzz_path=tmp_path / "oss-fuzz",
        inc_image_policy="pull_only",
        inc_image_registry="ghcr.io/example/custom",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=77,
        local_image_prefix="custom-prefix",
    )

    first_spec = next(specs)

    assert first_spec == WarmupBuildSpec(
        job_id="build-single/bench/variant-a/pkgs/inc/local/eval-1",
        payload={"kind": "build-a"},
        meta={
            "experiment_name": "exp-test",
            "warmup": "true",
            SCHEDULER_OWNER_KEY_META: build_scheduler_owner_key_for_ci_job(
                job_a,
                experiment_name="exp-test",
            ),
        },
    )
    mock_planner_cls.assert_called_once_with(
        tmp_path / "oss-fuzz",
        source_mode="pkgs",
    )
    mock_filter.assert_not_called()
    planner.iter_builds.assert_called_once_with(
        benchmark_path_a,
        use_inc_build=True,
        skip_if_cached=True,
        inc_image_policy="pull_only",
        inc_image_registry="ghcr.io/example/custom",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=77,
        local_image_prefix="custom-prefix",
    )

    second_spec = next(specs)

    assert second_spec == WarmupBuildSpec(
        job_id="build-single/bench/variant-b/pkgs/inc/local/eval-1",
        payload={"kind": "build-b"},
        meta={
            "experiment_name": "exp-test",
            "warmup": "true",
            SCHEDULER_OWNER_KEY_META: build_scheduler_owner_key_for_ci_job(
                job_b,
                experiment_name="exp-test",
            ),
        },
    )
    assert planner.iter_builds.call_args_list[1].args == (benchmark_path_b,)
    assert planner.iter_builds.call_args_list[1].kwargs == {
        "use_inc_build": True,
        "skip_if_cached": True,
        "inc_image_policy": "pull_only",
        "inc_image_registry": "ghcr.io/example/custom",
        "inc_image_max_pull_bytes": 123,
        "inc_image_pull_timeout": 77,
        "local_image_prefix": "custom-prefix",
    }


@patch("crsbench.distributed.ci_jobs.serialize_ci_job")
@patch("crsbench.executor.variant_planner.VariantPlanner")
def test_build_dispatcher_warmup_specs_uses_clean_builds_when_inc_build_disabled(
    mock_planner_cls: MagicMock,
    mock_serialize: MagicMock,
    tmp_path: Path,
) -> None:
    benchmark_name = "afc-mock-full-01"
    benchmark_path = tmp_path / benchmark_name
    benchmark_path.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(
        benchmarks_root=tmp_path,
        mode=SimpleNamespace(value="auto"),
        source_mode="pkgs",
        inc_build_enabled=False,
        get_benchmark_list=lambda: [benchmark_name],
    )
    planner = MagicMock()
    job = MagicMock()
    job.job_id = "build-single/bench/variant-clean"
    job.benchmark_name = "bench-clean"
    job.benchmark = "bench-clean"
    job.harness = "harness-clean"
    job.cpv_id = None
    job.sanitizer = "address"
    job.job_type = "build"
    planner.iter_builds.return_value = iter([job])
    mock_planner_cls.return_value = planner
    mock_serialize.return_value = {"kind": "build-clean"}

    specs = list(
        build_dispatcher_warmup_specs(
            config,
            experiment_name="exp-test",
            evaluator_id="eval-1",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123,
            inc_image_pull_timeout=77,
            local_image_prefix="custom-prefix",
        )
    )

    assert specs == [
        WarmupBuildSpec(
            job_id="build-single/bench/variant-clean/local/eval-1",
            payload={"kind": "build-clean"},
            meta={
                "experiment_name": "exp-test",
                "warmup": "true",
                SCHEDULER_OWNER_KEY_META: build_scheduler_owner_key_for_ci_job(
                    job,
                    experiment_name="exp-test",
                ),
            },
        )
    ]
    planner.iter_builds.assert_called_once_with(
        benchmark_path,
        use_inc_build=False,
        skip_if_cached=True,
        inc_image_policy="pull_only",
        inc_image_registry="ghcr.io/example/custom",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=77,
        local_image_prefix="custom-prefix",
    )


@patch("crsbench.distributed.ci_jobs.serialize_ci_job")
@patch("crsbench.executor.variant_planner.VariantPlanner")
def test_build_dispatcher_warmup_specs_consumes_one_job_at_a_time_within_benchmark(
    mock_planner_cls: MagicMock,
    mock_serialize: MagicMock,
    tmp_path: Path,
) -> None:
    class _CountingIterator:
        def __init__(self, jobs: list[MagicMock]) -> None:
            self._jobs = iter(jobs)
            self.next_calls = 0

        def __iter__(self) -> "_CountingIterator":
            return self

        def __next__(self) -> MagicMock:
            self.next_calls += 1
            return next(self._jobs)

    benchmark_name_a = "afc-mock-full-01"
    benchmark_name_b = "afc-mock-full-02"
    (tmp_path / benchmark_name_a).mkdir(parents=True, exist_ok=True)
    (tmp_path / benchmark_name_b).mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(
        benchmarks_root=tmp_path,
        mode=SimpleNamespace(value="auto"),
        source_mode="pkgs",
        get_benchmark_list=lambda: [benchmark_name_a, benchmark_name_b],
    )
    planner = MagicMock()
    job_a0 = MagicMock(job_id="build-a-0")
    job_a1 = MagicMock(job_id="build-a-1")
    job_b0 = MagicMock(job_id="build-b-0")
    first_iter = _CountingIterator([job_a0, job_a1])
    second_iter = _CountingIterator([job_b0])
    planner.iter_builds.side_effect = [first_iter, second_iter]
    mock_planner_cls.return_value = planner
    mock_serialize.side_effect = [{"kind": "a0"}, {"kind": "a1"}, {"kind": "b0"}]

    specs = build_dispatcher_warmup_specs(
        config,
        experiment_name="exp-test",
        evaluator_id="eval-1",
        oss_fuzz_path=tmp_path / "oss-fuzz",
        inc_image_policy="pull_only",
        inc_image_registry="ghcr.io/example/custom",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=77,
        local_image_prefix="custom-prefix",
    )

    assert next(specs).job_id == "build-a-0/local/eval-1"
    assert first_iter.next_calls == 1
    assert planner.iter_builds.call_count == 1

    assert next(specs).job_id == "build-a-1/local/eval-1"
    assert first_iter.next_calls == 2
    assert planner.iter_builds.call_count == 1

    assert next(specs).job_id == "build-b-0/local/eval-1"
    assert planner.iter_builds.call_count == 2
    assert second_iter.next_calls == 1


@patch("crsbench.distributed.ci_jobs.serialize_ci_job")
@patch("crsbench.executor.variant_planner.VariantPlanner")
def test_build_dispatcher_warmup_specs_localizes_prepare_dependencies(
    mock_planner_cls: MagicMock,
    mock_serialize: MagicMock,
    tmp_path: Path,
) -> None:
    benchmark_name = "afc-mock-full-01"
    benchmark_path = tmp_path / benchmark_name
    benchmark_path.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(
        benchmarks_root=tmp_path,
        mode=SimpleNamespace(value="auto"),
        source_mode="pkgs",
        get_benchmark_list=lambda: [benchmark_name],
    )
    planner = MagicMock()
    prepare_job = MagicMock()
    prepare_job.job_id = "prepare-inc-image/bench/address/pkgs/inc/cached"
    build_job = MagicMock()
    build_job.job_id = "build-single/bench/variant-a/pkgs/inc"
    build_job.prepare_inc_job_id = prepare_job.job_id
    planner.iter_builds.return_value = iter([prepare_job, build_job])
    mock_planner_cls.return_value = planner
    mock_serialize.side_effect = lambda job: {
        "job_id": job.job_id,
        "prepare_inc_job_id": getattr(job, "prepare_inc_job_id", ""),
    }

    specs = list(
        build_dispatcher_warmup_specs(
            config,
            experiment_name="exp-test",
            evaluator_id="eval-1",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123,
            inc_image_pull_timeout=77,
            local_image_prefix="custom-prefix",
        )
    )

    assert [spec.job_id for spec in specs] == [
        "prepare-inc-image/bench/address/pkgs/inc/cached/local/eval-1",
        "build-single/bench/variant-a/pkgs/inc/local/eval-1",
    ]
    assert specs[1].payload["prepare_inc_job_id"] == (
        "prepare-inc-image/bench/address/pkgs/inc/cached/local/eval-1"
    )


def test_feeder_enqueues_when_no_required_demand_and_spare_capacity_exists() -> None:
    queue = _FakeQueue(
        queued_job_ids=["queued-0"],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=_warmup_specs(3),
        build_capacity=3,
    )

    enqueued = feeder.tick()

    assert enqueued == 2
    assert [entry["job_id"] for entry in queue.enqueued] == [
        "warmup-build-0",
        "warmup-build-1",
    ]


def test_feeder_stops_when_required_demand_exists() -> None:
    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=True),
        warmup_specs=_warmup_specs(2),
        build_capacity=2,
    )

    enqueued = feeder.tick()

    assert enqueued == 0
    assert queue.enqueued == []


def test_feeder_resumes_after_required_demand_clears() -> None:
    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    state_store = _FakeStateStore(pending_required_builds=True)
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=state_store,
        warmup_specs=_warmup_specs(2),
        build_capacity=2,
    )

    assert feeder.tick() == 0
    assert queue.enqueued == []

    state_store.set_pending_required_builds(False)

    enqueued = feeder.tick()

    assert enqueued == 2
    assert [entry["job_id"] for entry in queue.enqueued] == [
        "warmup-build-0",
        "warmup-build-1",
    ]


def test_feeder_rechecks_required_demand_before_each_enqueue() -> None:
    class _FlippingStateStore:
        def __init__(self) -> None:
            self.calls = 0

        def has_pending_required_builds(self) -> bool:
            self.calls += 1
            return self.calls > 2

    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FlippingStateStore(),
        warmup_specs=_warmup_specs(2),
        build_capacity=2,
    )

    enqueued = feeder.tick()

    assert enqueued == 1
    assert [entry["job_id"] for entry in queue.enqueued] == ["warmup-build-0"]


def test_feeder_buffers_planned_spec_when_demand_flips_during_planning() -> None:
    class _MutableStateStore:
        def __init__(self) -> None:
            self.pending = False

        def has_pending_required_builds(self) -> bool:
            return self.pending

    state_store = _MutableStateStore()

    class _PlanningSpecs:
        def __init__(self) -> None:
            self._done = False

        def __iter__(self) -> "_PlanningSpecs":
            return self

        def __next__(self) -> WarmupBuildSpec:
            if self._done:
                raise StopIteration
            self._done = True
            state_store.pending = True
            return WarmupBuildSpec(
                job_id="warmup-build-0",
                payload={"id": 0},
                meta={"experiment_name": "exp-test", "warmup": "true"},
            )

    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=state_store,
        warmup_specs=_PlanningSpecs(),
        build_capacity=1,
    )

    assert feeder.tick() == 0
    assert queue.enqueued == []

    state_store.pending = False

    enqueued = feeder.tick()

    assert enqueued == 1
    assert [entry["job_id"] for entry in queue.enqueued] == ["warmup-build-0"]


def test_feeder_skips_warmup_while_claimed_request_is_materializing() -> None:
    from pathlib import Path

    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        _ActiveClaim,
    )
    from crsbench.distributed.evaluator_verify_claims import (
        VerifyClaim,
        VerifyRequestRecord,
    )

    claimed_record = VerifyRequestRecord(
        request_id="request-1",
        owner_key="trial::exp1::request-1",
        request_kind="pov",
        payload={"benchmark": "test-benchmark"},
        claim=VerifyClaim(evaluator_id="eval-1", expires_at=time.time() + 30.0),
    )
    worker = EvaluatorClaimWorker(
        redis_conn=MagicMock(),
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=MagicMock(),
        verify_queue=MagicMock(),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )
    worker.store.claim_next_request = MagicMock(return_value=claimed_record)
    worker.store.list_requests = MagicMock(return_value=[claimed_record])

    materialize_started = threading.Event()
    allow_materialize = threading.Event()

    def _materialize(_record: VerifyRequestRecord) -> _ActiveClaim:
        materialize_started.set()
        assert allow_materialize.wait(timeout=5)
        return _ActiveClaim(
            local_verify_job_id="verify-request-1",
            required_build_job_ids=(),
        )

    worker._materialize_claimed_request = _materialize  # type: ignore[method-assign]

    warmup_queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=warmup_queue,
        required_build_tracker=worker,
        warmup_specs=_warmup_specs(1),
        build_capacity=1,
    )

    dispatch_errors: list[BaseException] = []

    def _dispatch() -> None:
        try:
            worker.dispatch_one(now=100.0)
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            dispatch_errors.append(exc)

    dispatch_thread = threading.Thread(target=_dispatch)
    dispatch_thread.start()

    assert materialize_started.wait(timeout=5)
    assert feeder.tick() == 0
    assert warmup_queue.enqueued == []

    allow_materialize.set()
    dispatch_thread.join(timeout=5)

    assert not dispatch_thread.is_alive()
    assert dispatch_errors == []
    assert feeder.tick() == 1
    assert [entry["job_id"] for entry in warmup_queue.enqueued] == ["warmup-build-0"]


def test_feeder_rechecks_under_claim_gate_before_enqueue() -> None:
    from pathlib import Path

    from crsbench.distributed.evaluator_claim_worker import (
        EvaluatorClaimWorker,
        _ActiveClaim,
    )
    from crsbench.distributed.evaluator_verify_claims import (
        VerifyClaim,
        VerifyRequestRecord,
    )

    claimed_record = VerifyRequestRecord(
        request_id="request-1",
        owner_key="trial::exp1::request-1",
        request_kind="pov",
        payload={"benchmark": "test-benchmark"},
        claim=VerifyClaim(evaluator_id="eval-1", expires_at=time.time() + 30.0),
    )
    claim_visible = {"value": False}
    worker = EvaluatorClaimWorker(
        redis_conn=MagicMock(),
        experiment_name="exp1",
        evaluator_id="eval-1",
        build_queue=MagicMock(),
        verify_queue=MagicMock(),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )

    claim_started = threading.Event()
    allow_claim = threading.Event()
    materialize_started = threading.Event()
    allow_materialize = threading.Event()
    enqueue_guard_entered = threading.Event()

    def _claim_next_request(*, evaluator_id: str, now: float, lease_seconds: int):
        del evaluator_id, now, lease_seconds
        claim_started.set()
        assert allow_claim.wait(timeout=5)
        claim_visible["value"] = True
        return claimed_record

    def _list_requests() -> list[VerifyRequestRecord]:
        if claim_visible["value"]:
            return [claimed_record]
        return []

    def _materialize(_record: VerifyRequestRecord) -> _ActiveClaim:
        materialize_started.set()
        assert allow_materialize.wait(timeout=5)
        return _ActiveClaim(
            local_verify_job_id="verify-request-1",
            required_build_job_ids=(),
        )

    worker.store.claim_next_request = _claim_next_request  # type: ignore[method-assign]
    worker.store.list_requests = _list_requests  # type: ignore[method-assign]
    worker._materialize_claimed_request = _materialize  # type: ignore[method-assign]

    original_enqueue_if_idle = worker.enqueue_warmup_build_if_idle

    def _enqueue_warmup_build_if_idle(
        *, build_queue: Any, spec: WarmupBuildSpec
    ) -> bool:
        enqueue_guard_entered.set()
        return original_enqueue_if_idle(build_queue=build_queue, spec=spec)

    worker.enqueue_warmup_build_if_idle = _enqueue_warmup_build_if_idle  # type: ignore[method-assign]

    warmup_queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=warmup_queue,
        required_build_tracker=worker,
        warmup_specs=_warmup_specs(1),
        build_capacity=1,
    )

    dispatch_errors: list[BaseException] = []
    tick_errors: list[BaseException] = []
    tick_results: list[int] = []

    def _dispatch() -> None:
        try:
            worker.dispatch_one(now=100.0)
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            dispatch_errors.append(exc)

    def _tick() -> None:
        try:
            tick_results.append(feeder.tick())
        except BaseException as exc:  # pragma: no cover - assertion captures failure
            tick_errors.append(exc)

    dispatch_thread = threading.Thread(target=_dispatch)
    dispatch_thread.start()
    assert claim_started.wait(timeout=5)

    tick_thread = threading.Thread(target=_tick)
    tick_thread.start()
    assert enqueue_guard_entered.wait(timeout=5)

    allow_claim.set()
    assert materialize_started.wait(timeout=5)
    tick_thread.join(timeout=5)

    assert not tick_thread.is_alive()
    assert tick_errors == []
    assert tick_results == [0]
    assert warmup_queue.enqueued == []

    allow_materialize.set()
    dispatch_thread.join(timeout=5)

    assert not dispatch_thread.is_alive()
    assert dispatch_errors == []
    assert feeder.tick() == 1
    assert [entry["job_id"] for entry in warmup_queue.enqueued] == ["warmup-build-0"]


def test_feeder_respects_running_claimed_and_queued_capacity() -> None:
    queue = _FakeQueue(
        queued_job_ids=["queued-0"],
        intermediate_job_ids=["claim-a", "claim-b"],
        started_job_ids=["started-0"],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=_warmup_specs(4),
        build_capacity=5,
    )

    enqueued = feeder.tick()

    assert enqueued == 1
    assert [entry["job_id"] for entry in queue.enqueued] == ["warmup-build-0"]


def test_feeder_skips_duplicate_enqueue_errors() -> None:
    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    queue.enqueue = MagicMock(
        side_effect=[Exception("Job warmup-build-0 already exists"), None]
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=_warmup_specs(2),
        build_capacity=2,
    )

    enqueued = feeder.tick()

    assert enqueued == 1
    assert queue.enqueue.call_count == 2


def test_feeder_clears_buffered_spec_after_duplicate_enqueue() -> None:
    queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    queue.enqueue = MagicMock(
        side_effect=[
            Exception("Job warmup-build-0 already exists"),
            AssertionError("buffered duplicate warmup spec was retried"),
        ]
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=queue,
        state_store=_FakeStateStore(pending_required_builds=False),
        warmup_specs=(),
        build_capacity=1,
    )
    feeder._pending_spec = WarmupBuildSpec(
        job_id="warmup-build-0",
        payload={"id": 0},
        meta={"experiment_name": "exp-test", "warmup": "true"},
    )

    enqueued = feeder.tick()

    assert enqueued == 0
    assert feeder._pending_spec is None
    assert queue.enqueue.call_count == 1


def test_feeder_integrates_with_real_claim_store_before_enqueuing() -> None:
    from pathlib import Path

    from crsbench.distributed.evaluator_claim_worker import EvaluatorClaimWorker
    from crsbench.distributed.evaluator_verify_claims import (
        VerifyClaim,
        VerifyRequestRecord,
    )

    from tests.test_evaluator_claim_worker import _FakeRedis

    request_id = "request-1"
    redis_conn = _FakeRedis()
    worker = EvaluatorClaimWorker(
        redis_conn=redis_conn,
        experiment_name="exp-test",
        evaluator_id="eval-1",
        build_queue=MagicMock(),
        verify_queue=MagicMock(),
        verification_engine=MagicMock(),
        benchmarks_root=Path("/benchmarks"),
        max_inflight_requests=1,
    )
    worker.store.submit_request(
        VerifyRequestRecord(
            request_id=request_id,
            owner_key="trial::exp-test::request-1",
            request_kind="pov",
            payload={"benchmark": "test-benchmark"},
            claim=VerifyClaim(
                evaluator_id="eval-1",
                expires_at=time.time() + 30.0,
            ),
        )
    )

    warmup_queue = _FakeQueue(
        queued_job_ids=[],
        intermediate_job_ids=[],
        started_job_ids=[],
    )
    feeder = DispatcherWarmupFeeder(
        build_queue=warmup_queue,
        required_build_tracker=worker,
        warmup_specs=[
            WarmupBuildSpec(
                job_id="warmup-build-0",
                payload={"id": 0},
                meta={"experiment_name": "exp-test", "warmup": "true"},
            )
        ],
        build_capacity=1,
    )

    assert feeder.tick() == 0
    assert warmup_queue.enqueued == []

    assert worker.store.release_claim_if_current(
        request_id=request_id,
        evaluator_id="eval-1",
    )

    assert feeder.tick() == 1
    assert warmup_queue.enqueued == [
        {
            "func_name": "crsbench.distributed.build_jobs.execute_ci_build",
            "payload": {"id": 0},
            "job_timeout": 3600,
            "result_ttl": -1,
            "job_id": "warmup-build-0",
            "meta": {"experiment_name": "exp-test", "warmup": "true"},
        }
    ]
