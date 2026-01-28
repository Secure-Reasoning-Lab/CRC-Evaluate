"""Tests for DAG executor scheduling.

Tests cover:
- Dependency ordering (linear chains, diamond DAGs, fan-out)
- Parallelism bounds (max_workers respected)
- Failure propagation (direct, transitive, partial)
- Error detection (cycles, unknown deps)
- Edge cases (empty, all-fail, large DAGs)
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest
from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.executor import (
    CycleError,
    DAGExecutor,
    DependencyError,
    JobStatus,
)

# --- Helpers ---


@dataclass
class MockJob(Job):
    """Concrete Job for testing."""

    job_id_val: str
    deps: list[str]
    execute_fn: Optional[object] = None

    @property
    def job_id(self) -> str:
        return self.job_id_val

    @property
    def job_type(self) -> str:
        return "mock"

    @property
    def depends_on(self) -> list[str]:
        return self.deps

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()
        if self.execute_fn is not None:
            self.execute_fn()
        finished_at = datetime.now()
        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=True,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=(finished_at - started_at).total_seconds(),
        )


@dataclass
class FailingJob(Job):
    """Job that always fails."""

    job_id_val: str
    deps: list[str]
    error_msg: str = "job failed"

    @property
    def job_id(self) -> str:
        return self.job_id_val

    @property
    def job_type(self) -> str:
        return "mock-fail"

    @property
    def depends_on(self) -> list[str]:
        return self.deps

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()
        finished_at = datetime.now()
        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=False,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=0.0,
            error=self.error_msg,
        )


@dataclass
class ExplodingJob(Job):
    """Job that raises an exception."""

    job_id_val: str
    deps: list[str]

    @property
    def job_id(self) -> str:
        return self.job_id_val

    @property
    def job_type(self) -> str:
        return "mock-explode"

    @property
    def depends_on(self) -> list[str]:
        return self.deps

    def execute(self, context: JobContext) -> JobResult:
        raise RuntimeError("unexpected explosion")


@pytest.fixture
def mock_context() -> JobContext:
    return MagicMock(spec=JobContext)


# --- TestDAGExecutorBasic ---


class TestDAGExecutorBasic:
    def test_empty_jobs(self, mock_context: JobContext) -> None:
        executor = DAGExecutor(max_workers=2)
        results = executor.execute([], mock_context)
        assert results == {}

    def test_single_job(self, mock_context: JobContext) -> None:
        job = MockJob(job_id_val="a", deps=[])
        executor = DAGExecutor(max_workers=2)
        results = executor.execute([job], mock_context)

        assert "a" in results
        assert results["a"].status == JobStatus.SUCCESS
        assert results["a"].success is True
        assert results["a"].job_result is not None

    def test_single_job_failure(self, mock_context: JobContext) -> None:
        job = FailingJob(job_id_val="a", deps=[])
        executor = DAGExecutor(max_workers=2)
        results = executor.execute([job], mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert results["a"].success is False
        assert results["a"].error == "job failed"


# --- TestDAGExecutorDependencyOrder ---


class TestDAGExecutorDependencyOrder:
    def test_linear_chain(self, mock_context: JobContext) -> None:
        """A -> B -> C must execute in order."""
        order: list[str] = []
        lock = threading.Lock()

        def make_fn(name: str):
            def fn():
                with lock:
                    order.append(name)

            return fn

        jobs = [
            MockJob(job_id_val="a", deps=[], execute_fn=make_fn("a")),
            MockJob(job_id_val="b", deps=["a"], execute_fn=make_fn("b")),
            MockJob(job_id_val="c", deps=["b"], execute_fn=make_fn("c")),
        ]

        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert order.index("a") < order.index("b") < order.index("c")
        assert all(r.status == JobStatus.SUCCESS for r in results.values())

    def test_diamond_dag(self, mock_context: JobContext) -> None:
        """A -> B, A -> C, B -> D, C -> D."""
        order: list[str] = []
        lock = threading.Lock()

        def make_fn(name: str):
            def fn():
                with lock:
                    order.append(name)

            return fn

        jobs = [
            MockJob(job_id_val="a", deps=[], execute_fn=make_fn("a")),
            MockJob(job_id_val="b", deps=["a"], execute_fn=make_fn("b")),
            MockJob(job_id_val="c", deps=["a"], execute_fn=make_fn("c")),
            MockJob(job_id_val="d", deps=["b", "c"], execute_fn=make_fn("d")),
        ]

        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_independent_jobs(self, mock_context: JobContext) -> None:
        """4 independent jobs all execute."""
        jobs = [MockJob(job_id_val=f"j{i}", deps=[]) for i in range(4)]
        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert len(results) == 4
        assert all(r.status == JobStatus.SUCCESS for r in results.values())

    def test_mixed_deps_and_independent(self, mock_context: JobContext) -> None:
        """Some jobs depend, some don't. Deps are respected."""
        order: list[str] = []
        lock = threading.Lock()

        def make_fn(name: str):
            def fn():
                with lock:
                    order.append(name)

            return fn

        jobs = [
            MockJob(job_id_val="root", deps=[], execute_fn=make_fn("root")),
            MockJob(job_id_val="child", deps=["root"], execute_fn=make_fn("child")),
            MockJob(job_id_val="free1", deps=[], execute_fn=make_fn("free1")),
            MockJob(job_id_val="free2", deps=[], execute_fn=make_fn("free2")),
        ]

        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert order.index("root") < order.index("child")
        assert all(r.status == JobStatus.SUCCESS for r in results.values())


# --- TestDAGExecutorParallelism ---


class TestDAGExecutorParallelism:
    def test_max_parallel_respected(self, mock_context: JobContext) -> None:
        """Never exceed max_workers concurrent jobs."""
        max_workers = 2
        max_observed = [0]
        current = [0]
        lock = threading.Lock()

        def track_fn():
            with lock:
                current[0] += 1
                if current[0] > max_observed[0]:
                    max_observed[0] = current[0]
            time.sleep(0.02)
            with lock:
                current[0] -= 1

        jobs = [
            MockJob(job_id_val=f"j{i}", deps=[], execute_fn=track_fn) for i in range(6)
        ]

        executor = DAGExecutor(max_workers=max_workers)
        executor.execute(jobs, mock_context)

        assert max_observed[0] <= max_workers

    def test_max_workers_validation(self) -> None:
        """max_workers must be >= 1."""
        with pytest.raises(ValueError):
            DAGExecutor(max_workers=0)
        with pytest.raises(ValueError):
            DAGExecutor(max_workers=-1)

    def test_parallelism_utilized(self, mock_context: JobContext) -> None:
        """Independent jobs run in parallel, not sequentially."""
        job_duration = 0.05
        num_jobs = 4

        def slow_fn():
            time.sleep(job_duration)

        jobs = [
            MockJob(job_id_val=f"j{i}", deps=[], execute_fn=slow_fn)
            for i in range(num_jobs)
        ]

        executor = DAGExecutor(max_workers=num_jobs)
        start = time.monotonic()
        executor.execute(jobs, mock_context)
        total = time.monotonic() - start

        # Should be roughly 1x job_duration, not 4x
        assert total < job_duration * num_jobs * 0.8


# --- TestDAGExecutorFailurePropagation ---


class TestDAGExecutorFailurePropagation:
    def test_direct_dep_failure(self, mock_context: JobContext) -> None:
        """A fails -> B (depends on A) gets DEP_FAILED."""
        jobs: list[Job] = [
            FailingJob(job_id_val="a", deps=[]),
            MockJob(job_id_val="b", deps=["a"]),
        ]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert results["b"].status == JobStatus.DEP_FAILED

    def test_transitive_dep_failure(self, mock_context: JobContext) -> None:
        """A fails -> B DEP_FAILED -> C DEP_FAILED."""
        jobs: list[Job] = [
            FailingJob(job_id_val="a", deps=[]),
            MockJob(job_id_val="b", deps=["a"]),
            MockJob(job_id_val="c", deps=["b"]),
        ]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert results["b"].status == JobStatus.DEP_FAILED
        assert results["c"].status == JobStatus.DEP_FAILED

    def test_partial_failure(self, mock_context: JobContext) -> None:
        """A fails, C is independent -> C still runs."""
        jobs: list[Job] = [
            FailingJob(job_id_val="a", deps=[]),
            MockJob(job_id_val="b", deps=["a"]),
            MockJob(job_id_val="c", deps=[]),
        ]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert results["b"].status == JobStatus.DEP_FAILED
        assert results["c"].status == JobStatus.SUCCESS

    def test_failure_with_multiple_deps(self, mock_context: JobContext) -> None:
        """D depends on [A, B]. A fails -> D is DEP_FAILED even though B succeeds."""
        jobs: list[Job] = [
            FailingJob(job_id_val="a", deps=[]),
            MockJob(job_id_val="b", deps=[]),
            MockJob(job_id_val="d", deps=["a", "b"]),
        ]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert results["b"].status == JobStatus.SUCCESS
        assert results["d"].status == JobStatus.DEP_FAILED

    def test_dep_failed_jobs_not_executed(self, mock_context: JobContext) -> None:
        """DEP_FAILED jobs never have execute() called."""
        executed: list[str] = []
        lock = threading.Lock()

        def track_fn(name: str):
            def fn():
                with lock:
                    executed.append(name)

            return fn

        jobs: list[Job] = [
            FailingJob(job_id_val="a", deps=[]),
            MockJob(job_id_val="b", deps=["a"], execute_fn=track_fn("b")),
            MockJob(job_id_val="c", deps=["b"], execute_fn=track_fn("c")),
        ]

        executor = DAGExecutor(max_workers=2)
        executor.execute(jobs, mock_context)

        assert "b" not in executed
        assert "c" not in executed


# --- TestDAGExecutorErrors ---


class TestDAGExecutorErrors:
    def test_cycle_detection(self, mock_context: JobContext) -> None:
        """A -> B -> A raises CycleError."""
        jobs = [
            MockJob(job_id_val="a", deps=["b"]),
            MockJob(job_id_val="b", deps=["a"]),
        ]

        executor = DAGExecutor(max_workers=2)
        with pytest.raises(CycleError):
            executor.execute(jobs, mock_context)

    def test_unknown_dependency(self, mock_context: JobContext) -> None:
        """Job depends on non-existent ID raises DependencyError."""
        jobs = [
            MockJob(job_id_val="a", deps=["nonexistent"]),
        ]

        executor = DAGExecutor(max_workers=2)
        with pytest.raises(DependencyError) as exc_info:
            executor.execute(jobs, mock_context)

        assert exc_info.value.job_id == "a"
        assert exc_info.value.unknown_dep == "nonexistent"

    def test_self_dependency(self, mock_context: JobContext) -> None:
        """Job depends on itself raises CycleError."""
        jobs = [
            MockJob(job_id_val="a", deps=["a"]),
        ]

        executor = DAGExecutor(max_workers=2)
        with pytest.raises(CycleError):
            executor.execute(jobs, mock_context)


# --- TestDAGExecutorEdgeCases ---


class TestDAGExecutorEdgeCases:
    def test_job_raises_exception(self, mock_context: JobContext) -> None:
        """Job execute() raising exception results in FAILED."""
        jobs: list[Job] = [
            ExplodingJob(job_id_val="a", deps=[]),
        ]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert results["a"].status == JobStatus.FAILED
        assert "unexpected explosion" in (results["a"].error or "")

    def test_all_jobs_fail(self, mock_context: JobContext) -> None:
        """Multiple independent jobs all fail."""
        jobs: list[Job] = [FailingJob(job_id_val=f"j{i}", deps=[]) for i in range(3)]

        executor = DAGExecutor(max_workers=2)
        results = executor.execute(jobs, mock_context)

        assert all(r.status == JobStatus.FAILED for r in results.values())

    def test_large_dag(self, mock_context: JobContext) -> None:
        """Fan-out DAG: 1 root -> 10 leaves -> 1 sink."""
        order: list[str] = []
        lock = threading.Lock()

        def make_fn(name: str):
            def fn():
                with lock:
                    order.append(name)

            return fn

        root = MockJob(job_id_val="root", deps=[], execute_fn=make_fn("root"))
        leaves = [
            MockJob(
                job_id_val=f"leaf{i}",
                deps=["root"],
                execute_fn=make_fn(f"leaf{i}"),
            )
            for i in range(10)
        ]
        sink = MockJob(
            job_id_val="sink",
            deps=[f"leaf{i}" for i in range(10)],
            execute_fn=make_fn("sink"),
        )

        jobs: list[Job] = [root, *leaves, sink]
        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert order[0] == "root"
        assert order[-1] == "sink"
        assert all(r.status == JobStatus.SUCCESS for r in results.values())


# --- Typed Job Helper ---


@dataclass
class TypedJob(Job):
    """Job with configurable job_type for type_limits testing."""

    job_id_val: str
    deps: list[str]
    type_val: str = "default"
    execute_fn: Optional[object] = None

    @property
    def job_id(self) -> str:
        return self.job_id_val

    @property
    def job_type(self) -> str:
        return self.type_val

    @property
    def depends_on(self) -> list[str]:
        return self.deps

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()
        if self.execute_fn is not None:
            self.execute_fn()
        finished_at = datetime.now()
        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=True,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=(finished_at - started_at).total_seconds(),
        )


# --- TestDAGExecutorTypeLimits ---


class TestDAGExecutorTypeLimits:
    """Tests for typed concurrency limits."""

    def test_type_limits_basic(self, mock_context: JobContext) -> None:
        """type_limits={"build": 1, "verify": 2} limits concurrency by type."""
        max_build_concurrent = 0
        max_verify_concurrent = 0
        build_concurrent = 0
        verify_concurrent = 0
        lock = threading.Lock()

        def build_fn():
            nonlocal build_concurrent, max_build_concurrent
            with lock:
                build_concurrent += 1
                max_build_concurrent = max(max_build_concurrent, build_concurrent)
            time.sleep(0.05)
            with lock:
                build_concurrent -= 1

        def verify_fn():
            nonlocal verify_concurrent, max_verify_concurrent
            with lock:
                verify_concurrent += 1
                max_verify_concurrent = max(max_verify_concurrent, verify_concurrent)
            time.sleep(0.05)
            with lock:
                verify_concurrent -= 1

        jobs: list[Job] = [
            TypedJob(job_id_val="b1", deps=[], type_val="build", execute_fn=build_fn),
            TypedJob(job_id_val="b2", deps=[], type_val="build", execute_fn=build_fn),
            TypedJob(job_id_val="b3", deps=[], type_val="build", execute_fn=build_fn),
            TypedJob(job_id_val="v1", deps=[], type_val="verify", execute_fn=verify_fn),
            TypedJob(job_id_val="v2", deps=[], type_val="verify", execute_fn=verify_fn),
            TypedJob(job_id_val="v3", deps=[], type_val="verify", execute_fn=verify_fn),
            TypedJob(job_id_val="v4", deps=[], type_val="verify", execute_fn=verify_fn),
        ]

        executor = DAGExecutor(type_limits={"build": 1, "verify": 2})
        results = executor.execute(jobs, mock_context)

        assert all(r.status == JobStatus.SUCCESS for r in results.values())
        assert max_build_concurrent <= 1
        assert max_verify_concurrent <= 2

    def test_type_limits_pool_size(self) -> None:
        """Pool size = sum of type_limits values."""
        executor = DAGExecutor(type_limits={"build": 2, "verify": 8})
        assert executor.max_workers == 10

    def test_type_limits_validation(self) -> None:
        """Zero-sum type_limits values raise ValueError."""
        with pytest.raises(ValueError, match="sum of type_limits must be >= 1"):
            DAGExecutor(type_limits={"build": 0})

    def test_no_type_limits_backward_compat(self, mock_context: JobContext) -> None:
        """Without type_limits, existing max_workers behavior unchanged."""
        jobs: list[Job] = [
            TypedJob(job_id_val="a", deps=[], type_val="build"),
            TypedJob(job_id_val="b", deps=[], type_val="verify"),
        ]

        executor = DAGExecutor(max_workers=4)
        results = executor.execute(jobs, mock_context)

        assert all(r.status == JobStatus.SUCCESS for r in results.values())

    def test_dep_failed_with_type_limits(self, mock_context: JobContext) -> None:
        """DEP_FAILED propagation works correctly with type_limits."""
        jobs: list[Job] = [
            FailingJob(job_id_val="build1", deps=[]),
            TypedJob(job_id_val="verify1", deps=["build1"], type_val="verify"),
            TypedJob(job_id_val="verify2", deps=["verify1"], type_val="verify"),
        ]

        executor = DAGExecutor(type_limits={"build": 1, "verify": 2})
        results = executor.execute(jobs, mock_context)

        assert results["build1"].status == JobStatus.FAILED
        assert results["verify1"].status == JobStatus.DEP_FAILED
        assert results["verify2"].status == JobStatus.DEP_FAILED

    def test_unknown_type_unlimited(self, mock_context: JobContext) -> None:
        """Jobs with types not in type_limits are treated as unlimited."""
        jobs: list[Job] = [
            TypedJob(job_id_val="u1", deps=[], type_val="unknown"),
            TypedJob(job_id_val="u2", deps=[], type_val="unknown"),
            TypedJob(job_id_val="u3", deps=[], type_val="unknown"),
        ]

        executor = DAGExecutor(type_limits={"build": 1})
        results = executor.execute(jobs, mock_context)

        assert all(r.status == JobStatus.SUCCESS for r in results.values())

    def test_deferred_build_runs_after_slot_frees(
        self, mock_context: JobContext
    ) -> None:
        """Build job held in buffer runs after existing build completes."""
        order: list[str] = []
        lock = threading.Lock()

        def track(name: str):
            def fn():
                with lock:
                    order.append(name)
                time.sleep(0.03)

            return fn

        jobs: list[Job] = [
            TypedJob(
                job_id_val="b1", deps=[], type_val="build", execute_fn=track("b1")
            ),
            TypedJob(
                job_id_val="b2", deps=[], type_val="build", execute_fn=track("b2")
            ),
        ]

        executor = DAGExecutor(type_limits={"build": 1})
        results = executor.execute(jobs, mock_context)

        assert all(r.status == JobStatus.SUCCESS for r in results.values())
        # Both completed (order doesn't matter, but both ran)
        assert set(order) == {"b1", "b2"}
