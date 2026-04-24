"""Tests for dispatcher-local evaluator warmup feeder."""

from __future__ import annotations

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
    planner.plan_builds.side_effect = [[job_a], [job_b]]
    mock_planner_cls.return_value = planner
    mock_filter.side_effect = lambda names, _mode, _root: names
    mock_serialize.side_effect = [{"kind": "build-a"}, {"kind": "build-b"}]

    specs = build_dispatcher_warmup_specs(
        config,
        experiment_name="exp-test",
        oss_fuzz_path=tmp_path / "oss-fuzz",
        inc_image_policy="pull_only",
        inc_image_registry="ghcr.io/example/custom",
        inc_image_max_pull_bytes=123,
        inc_image_pull_timeout=77,
        local_image_prefix="custom-prefix",
    )

    first_spec = next(specs)

    assert first_spec == WarmupBuildSpec(
        job_id="build-single/bench/variant-a/pkgs/inc",
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
    planner.plan_builds.assert_called_once_with(
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
        job_id="build-single/bench/variant-b/pkgs/inc",
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
    assert planner.plan_builds.call_args_list[1].args == (benchmark_path_b,)
    assert planner.plan_builds.call_args_list[1].kwargs == {
        "use_inc_build": True,
        "skip_if_cached": True,
        "inc_image_policy": "pull_only",
        "inc_image_registry": "ghcr.io/example/custom",
        "inc_image_max_pull_bytes": 123,
        "inc_image_pull_timeout": 77,
        "local_image_prefix": "custom-prefix",
    }


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
            return self.calls > 1

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
