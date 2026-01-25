"""Tests for crsbench.experiment.build_orchestrator.

Tests verify:
- Deduplication of build jobs by (benchmark_path, sanitizer)
- Job ID format and job type
- Context population during build execution
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.jobs.base import JobContext, JobResult
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.experiment.build_orchestrator import (
    create_upfront_build_jobs,
    execute_upfront_builds,
)


@dataclass
class MockBenchmarkHarness:
    """Mock benchmark harness for testing."""

    path: Path
    harness: str = "fuzz_target"


@dataclass
class MockTrial:
    """Mock Trial for testing.

    Mimics the Trial class from crsbench.run_experiment with minimal fields.
    """

    benchmark_harness: MockBenchmarkHarness
    sanitizer: str = "address"


class TestCreateUpfrontBuildJobs:
    """Tests for create_upfront_build_jobs function."""

    def test_deduplication_same_benchmark_different_trials(self) -> None:
        """Jobs are deduplicated when multiple trials share same benchmark."""
        benchmark = MockBenchmarkHarness(path=Path("/benchmarks/curl"))
        trials = [
            MockTrial(benchmark_harness=benchmark, sanitizer="address"),
            MockTrial(benchmark_harness=benchmark, sanitizer="address"),
            MockTrial(benchmark_harness=benchmark, sanitizer="address"),
        ]

        jobs = create_upfront_build_jobs(trials)

        assert len(jobs) == 1
        assert jobs[0].benchmark_name == "curl"

    def test_deduplication_different_sanitizers(self) -> None:
        """Different sanitizers create separate jobs."""
        benchmark = MockBenchmarkHarness(path=Path("/benchmarks/curl"))
        trials = [
            MockTrial(benchmark_harness=benchmark, sanitizer="address"),
            MockTrial(benchmark_harness=benchmark, sanitizer="undefined"),
        ]

        jobs = create_upfront_build_jobs(trials)

        assert len(jobs) == 2

    def test_deduplication_different_benchmarks(self) -> None:
        """Different benchmarks create separate jobs."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/nginx")),
                sanitizer="address",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)

        assert len(jobs) == 2
        benchmark_names = {job.benchmark_name for job in jobs}
        assert benchmark_names == {"curl", "nginx"}

    def test_deduplication_complex_scenario(self) -> None:
        """Complex deduplication with multiple benchmarks and sanitizers."""
        curl_path = Path("/benchmarks/curl")
        nginx_path = Path("/benchmarks/nginx")

        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=curl_path),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=curl_path),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=nginx_path),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=curl_path),
                sanitizer="undefined",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)

        # Should have 3 unique combinations:
        # (curl, address), (nginx, address), (curl, undefined)
        assert len(jobs) == 3

    def test_job_ids_follow_format(self) -> None:
        """Job IDs follow expected format."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)

        assert len(jobs) == 1
        assert jobs[0].job_id == "build-variants:curl"

    def test_job_type_is_build(self) -> None:
        """All jobs have job_type 'build'."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/nginx")),
                sanitizer="address",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)

        for job in jobs:
            assert job.job_type == "build"

    def test_inc_build_flag_passed(self) -> None:
        """use_inc_build flag is passed to jobs."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
        ]

        jobs_with_inc = create_upfront_build_jobs(trials, use_inc_build=True)
        jobs_without_inc = create_upfront_build_jobs(trials, use_inc_build=False)

        assert jobs_with_inc[0].use_inc_build is True
        assert jobs_without_inc[0].use_inc_build is False

    def test_force_rebuild_flag_passed(self) -> None:
        """force_rebuild flag is passed to jobs."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
        ]

        jobs_normal = create_upfront_build_jobs(trials, force_rebuild=False)
        jobs_force = create_upfront_build_jobs(trials, force_rebuild=True)

        assert jobs_normal[0].force_rebuild is False
        assert jobs_force[0].force_rebuild is True

    def test_empty_trials_returns_empty_jobs(self) -> None:
        """Empty trial list returns empty job list."""
        jobs = create_upfront_build_jobs([])
        assert jobs == []

    def test_trial_without_benchmark_harness_skipped(self) -> None:
        """Trials missing benchmark_harness are skipped with warning."""

        @dataclass
        class InvalidTrial:
            sanitizer: str = "address"

        trials = [InvalidTrial()]  # type: ignore[list-item]
        jobs = create_upfront_build_jobs(trials)
        assert jobs == []

    def test_source_mode_set_to_main_repo(self) -> None:
        """Jobs have source_mode set to 'main_repo'."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=Path("/benchmarks/curl")),
                sanitizer="address",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)

        assert jobs[0].source_mode == "main_repo"


class TestExecuteUpfrontBuilds:
    """Tests for execute_upfront_builds function."""

    def test_empty_jobs_returns_empty_results(self) -> None:
        """Empty job list returns empty results dict and fresh context."""
        results, context = execute_upfront_builds([])

        assert results == {}
        assert isinstance(context, JobContext)
        assert context.shared == {}

    def test_context_population_on_success(self) -> None:
        """Context.shared is populated after successful execution."""
        from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob

        job = BuildVariantsJob(
            benchmark_path=Path("/benchmarks/curl"),
            benchmark_name="curl",
            use_inc_build=True,
            force_rebuild=False,
            source_mode="main_repo",
        )

        # Mock the DAGExecutor to simulate execution
        mock_result = ExecutorResult(
            job_id="build-variants:curl",
            status=JobStatus.SUCCESS,
            elapsed_seconds=10.0,
            job_result=JobResult(
                job_id="build-variants:curl",
                job_type="build",
                success=True,
                started_at=datetime.now(),
                finished_at=datetime.now(),
                elapsed_seconds=10.0,
            ),
        )

        with patch(
            "crsbench.experiment.build_orchestrator.DAGExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Simulate context.shared being populated during execution
            def mock_execute(
                _jobs: list[Any], context: JobContext
            ) -> dict[str, ExecutorResult]:
                context.shared["build-variants:curl"] = {
                    "build_results": {"asan": "result"},
                    "adapter": "mock_adapter",
                }
                return {"build-variants:curl": mock_result}

            mock_executor.execute.side_effect = mock_execute

            results, context = execute_upfront_builds([job], build_workers=2)

        assert "build-variants:curl" in context.shared
        assert results["build-variants:curl"].success

    def test_executor_uses_typed_limits(self) -> None:
        """Executor is created with type_limits for build workers."""
        from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob

        job = BuildVariantsJob(
            benchmark_path=Path("/benchmarks/curl"),
            benchmark_name="curl",
            use_inc_build=True,
            force_rebuild=False,
            source_mode="main_repo",
        )

        with patch(
            "crsbench.experiment.build_orchestrator.DAGExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            mock_executor.execute.return_value = {}

            execute_upfront_builds([job], build_workers=4)

            mock_executor_class.assert_called_once_with(type_limits={"build": 4})

    def test_results_track_success_failure(self) -> None:
        """Results correctly track success and failure counts."""
        from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob

        jobs = [
            BuildVariantsJob(
                benchmark_path=Path("/benchmarks/curl"),
                benchmark_name="curl",
                use_inc_build=True,
                force_rebuild=False,
                source_mode="main_repo",
            ),
            BuildVariantsJob(
                benchmark_path=Path("/benchmarks/nginx"),
                benchmark_name="nginx",
                use_inc_build=True,
                force_rebuild=False,
                source_mode="main_repo",
            ),
        ]

        mock_results = {
            "build-variants:curl": ExecutorResult(
                job_id="build-variants:curl",
                status=JobStatus.SUCCESS,
                elapsed_seconds=10.0,
            ),
            "build-variants:nginx": ExecutorResult(
                job_id="build-variants:nginx",
                status=JobStatus.FAILED,
                elapsed_seconds=5.0,
                error="Build failed",
            ),
        }

        with patch(
            "crsbench.experiment.build_orchestrator.DAGExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            mock_executor.execute.return_value = mock_results

            results, _ = execute_upfront_builds(jobs, build_workers=2)

        assert results["build-variants:curl"].success
        assert not results["build-variants:nginx"].success

    def test_returned_context_can_be_reused(self) -> None:
        """Returned context can be reused for downstream jobs."""
        with patch(
            "crsbench.experiment.build_orchestrator.DAGExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            mock_executor.execute.return_value = {}

            results, context = execute_upfront_builds([])

            # Context should be usable for further jobs
            assert context.builder is None
            assert context.infra is None
            assert isinstance(context.shared, dict)
