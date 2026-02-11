"""Tests for the local sequential job executor.

Tests verify:
- Jobs execute in dependency order
- context.shared populated for downstream jobs
- Failed dependency -> DEP_FAILED
- Empty jobs list -> empty results
- Independent jobs all execute
- Exception handling during job execution
- --distributed flag parsing on CI subcommands
"""

import argparse
from datetime import datetime

import pytest
from crsbench.benchmark_ci.executor import execute_jobs_locally
from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.executor.types import JobStatus


class StubJob(Job):
    """Minimal Job stub for testing."""

    def __init__(
        self,
        job_id_str: str,
        job_type_str: str = "test",
        deps: list[str] | None = None,
        *,
        succeed: bool = True,
        details: dict | None = None,
        raise_exc: Exception | None = None,
    ):
        self._job_id = job_id_str
        self._job_type = job_type_str
        self._deps = deps or []
        self._succeed = succeed
        self._details = details or {}
        self._raise_exc = raise_exc

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def job_type(self) -> str:
        return self._job_type

    @property
    def depends_on(self) -> list[str]:
        return self._deps

    def execute(self, context: JobContext) -> JobResult:
        if self._raise_exc:
            raise self._raise_exc
        now = datetime.now()
        return JobResult(
            job_id=self._job_id,
            job_type=self._job_type,
            success=self._succeed,
            started_at=now,
            finished_at=now,
            elapsed_seconds=0.01,
            details=self._details,
            error=None if self._succeed else "job failed",
        )


class TestExecuteJobsLocally:
    """Tests for execute_jobs_locally()."""

    def test_empty_jobs_returns_empty(self):
        result = execute_jobs_locally([])
        assert result == {}

    def test_single_job_success(self):
        job = StubJob("build/bench1/variant1")
        results = execute_jobs_locally([job])

        assert "build/bench1/variant1" in results
        r = results["build/bench1/variant1"]
        assert r.status == JobStatus.SUCCESS
        assert r.job_result is not None
        assert r.job_result.success is True

    def test_single_job_failure(self):
        job = StubJob("build/bench1/variant1", succeed=False)
        results = execute_jobs_locally([job])

        r = results["build/bench1/variant1"]
        assert r.status == JobStatus.FAILED

    def test_dependency_order(self):
        """Downstream job executes after its dependency."""
        build_job = StubJob("build/bench1/variant1", details={"image": "test-image"})
        verify_job = StubJob(
            "verify/bench1/cpv_0",
            deps=["build/bench1/variant1"],
        )
        # Pass in reverse order — executor should still sort correctly
        results = execute_jobs_locally([verify_job, build_job])

        assert results["build/bench1/variant1"].status == JobStatus.SUCCESS
        assert results["verify/bench1/cpv_0"].status == JobStatus.SUCCESS

    def test_dep_failed_propagates(self):
        """If a dependency fails, downstream job is DEP_FAILED."""
        build_job = StubJob("build/bench1/variant1", succeed=False)
        verify_job = StubJob(
            "verify/bench1/cpv_0",
            deps=["build/bench1/variant1"],
        )
        results = execute_jobs_locally([build_job, verify_job])

        assert results["build/bench1/variant1"].status == JobStatus.FAILED
        assert results["verify/bench1/cpv_0"].status == JobStatus.DEP_FAILED

    def test_independent_jobs_all_execute(self):
        """Independent jobs all run even though executed sequentially."""
        jobs = [
            StubJob("build/bench1/variant1"),
            StubJob("build/bench1/variant2"),
            StubJob("build/bench2/variant1"),
        ]
        results = execute_jobs_locally(jobs)

        assert len(results) == 3
        for r in results.values():
            assert r.status == JobStatus.SUCCESS

    def test_context_shared_populated(self):
        """Successful job details are stored in context.shared."""
        ctx = JobContext()
        job = StubJob("build/bench1/variant1", details={"image": "test-image"})
        execute_jobs_locally([job], context=ctx)

        assert "build/bench1/variant1" in ctx.shared
        assert ctx.shared["build/bench1/variant1"]["image"] == "test-image"

    def test_failed_job_not_in_shared(self):
        """Failed job details are NOT stored in context.shared."""
        ctx = JobContext()
        job = StubJob("build/bench1/variant1", succeed=False)
        execute_jobs_locally([job], context=ctx)

        assert "build/bench1/variant1" not in ctx.shared

    def test_exception_during_execute(self):
        """Exception in job.execute() is caught and marked FAILED."""
        job = StubJob(
            "build/bench1/variant1",
            raise_exc=RuntimeError("Docker error"),
        )
        results = execute_jobs_locally([job])

        r = results["build/bench1/variant1"]
        assert r.status == JobStatus.FAILED
        assert "Docker error" in (r.error or "")

    def test_diamond_dag(self):
        """Diamond dependency pattern: A -> B,C -> D."""
        a = StubJob("a")
        b = StubJob("b", deps=["a"])
        c = StubJob("c", deps=["a"])
        d = StubJob("d", deps=["b", "c"])

        results = execute_jobs_locally([d, c, b, a])

        assert all(r.status == JobStatus.SUCCESS for r in results.values())
        assert len(results) == 4

    def test_diamond_dag_dep_failure(self):
        """Diamond: if B fails, D gets DEP_FAILED but C still runs."""
        a = StubJob("a")
        b = StubJob("b", deps=["a"], succeed=False)
        c = StubJob("c", deps=["a"])
        d = StubJob("d", deps=["b", "c"])

        results = execute_jobs_locally([a, b, c, d])

        assert results["a"].status == JobStatus.SUCCESS
        assert results["b"].status == JobStatus.FAILED
        assert results["c"].status == JobStatus.SUCCESS
        assert results["d"].status == JobStatus.DEP_FAILED

    def test_external_dependency_not_blocking(self):
        """Job with dependency outside job set still executes."""
        job = StubJob("verify/bench1/cpv_0", deps=["build/bench1/external"])
        results = execute_jobs_locally([job])

        # External dep not in job set — job should still run
        assert results["verify/bench1/cpv_0"].status == JobStatus.SUCCESS


class TestDistributedFlagParsing:
    """Test --distributed flag is parsed correctly on CI subcommands."""

    def _make_parser(self):
        from crsbench.benchmark_ci.cli import add_ci_subparser

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="command")
        add_ci_subparser(subs)
        return parser

    @pytest.mark.parametrize(
        "subcommand",
        ["pov", "patch", "coverage", "rts", "all"],
    )
    def test_distributed_default_false(self, subcommand):
        parser = self._make_parser()
        args = parser.parse_args(["ci", subcommand, "--all"])
        assert args.distributed is False

    @pytest.mark.parametrize(
        "subcommand",
        ["pov", "patch", "coverage", "rts", "all"],
    )
    def test_distributed_flag_sets_true(self, subcommand):
        parser = self._make_parser()
        args = parser.parse_args(["ci", subcommand, "--all", "--distributed"])
        assert args.distributed is True

    def test_build_distributed_default_false(self):
        parser = self._make_parser()
        args = parser.parse_args(["ci", "build", "--all"])
        assert args.distributed is False

    def test_build_distributed_flag_sets_true(self):
        parser = self._make_parser()
        args = parser.parse_args(["ci", "build", "--all", "--distributed"])
        assert args.distributed is True
