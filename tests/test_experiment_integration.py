"""Integration tests for the complete experiment flow.

Tests verify the complete flow:
1. Upfront builds via create_upfront_build_jobs + execute_upfront_builds
2. CRS trial execution via CRSRunJob
3. Post-trial analysis via create_post_trial_jobs + execute_post_trial_analysis
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.jobs.base import JobContext, JobResult
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
)
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.experiment import (
    CRSRunJob,
    TrialResult,
    create_post_trial_jobs,
    create_upfront_build_jobs,
    execute_post_trial_analysis,
    execute_upfront_builds,
)


@dataclass
class MockBenchmarkHarness:
    """Mock benchmark harness for testing."""

    path: Path
    harness: str = "fuzz_target"


@dataclass
class MockTrial:
    """Mock Trial for testing."""

    benchmark_harness: MockBenchmarkHarness
    sanitizer: str = "address"


class TestExperimentFlowMock:
    """Test complete experiment flow with mocked dependencies."""

    @patch("crsbench.experiment.build_orchestrator.DAGExecutor")
    @patch("crsbench.experiment.post_trial.DAGExecutor")
    def test_experiment_flow_mock(
        self,
        mock_post_trial_executor_cls: MagicMock,
        mock_build_executor_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test complete flow: upfront builds -> CRS trials -> post-trial."""
        # Setup benchmarks
        benchmark1_path = tmp_path / "benchmark1"
        benchmark2_path = tmp_path / "benchmark2"
        benchmark1_path.mkdir()
        benchmark2_path.mkdir()

        # --- Phase 1: Upfront Builds ---
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=benchmark1_path),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=benchmark1_path),
                sanitizer="address",
            ),
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=benchmark2_path),
                sanitizer="address",
            ),
        ]

        # Create build jobs (should deduplicate to 2)
        build_jobs = create_upfront_build_jobs(trials)
        assert len(build_jobs) == 2

        # Mock build execution
        mock_build_executor = MagicMock()
        mock_build_executor_cls.return_value = mock_build_executor

        def mock_build_execute(
            jobs: list[Any], context: JobContext
        ) -> dict[str, ExecutorResult]:
            results = {}
            for job in jobs:
                context.shared[job.job_id] = {
                    "adapter": MagicMock(),
                    "build_results": {},
                }
                results[job.job_id] = ExecutorResult(
                    job_id=job.job_id,
                    status=JobStatus.SUCCESS,
                    elapsed_seconds=30.0,
                    job_result=JobResult(
                        job_id=job.job_id,
                        job_type="build",
                        success=True,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                        elapsed_seconds=30.0,
                    ),
                )
            return results

        mock_build_executor.execute.side_effect = mock_build_execute

        # Execute builds
        build_results, context = execute_upfront_builds(build_jobs, build_workers=2)
        assert len(build_results) == 2
        assert all(r.success for r in build_results.values())

        # Verify context populated
        assert "build-variants:benchmark1" in context.shared
        assert "build-variants:benchmark2" in context.shared

        # --- Phase 2: CRS Trials (simulated via TrialResult) ---
        # In real flow, CRSRunJob would be executed here
        # We simulate the results

        trial_results = [
            TrialResult(
                trial_id="trial-1",
                benchmark_path=benchmark1_path,
                harness_name="fuzz_target",
                trial_output_dir=tmp_path / "trial1",
                success=True,
                crs_type="bug_finding",
                cpvs_found=["cpv_0"],
            ),
            TrialResult(
                trial_id="trial-2",
                benchmark_path=benchmark2_path,
                harness_name="fuzz_target",
                trial_output_dir=tmp_path / "trial2",
                success=False,  # Failed trial
                crs_type="bug_finding",
            ),
        ]

        # --- Phase 3: Post-Trial Analysis ---
        build_job_ids = {job.benchmark_name: job.job_id for job in build_jobs}

        post_trial_jobs = create_post_trial_jobs(
            trial_results,
            build_job_ids,
            coverage_enabled=True,
        )

        # Only 1 job (failed trial skipped)
        assert len(post_trial_jobs) == 1
        assert isinstance(post_trial_jobs[0], FlatCollectCoverageJob)
        assert post_trial_jobs[0].benchmark_name == "benchmark1"

        # Mock post-trial execution
        mock_post_trial_executor = MagicMock()
        mock_post_trial_executor_cls.return_value = mock_post_trial_executor

        mock_post_trial_executor.execute.return_value = {
            post_trial_jobs[0].job_id: ExecutorResult(
                job_id=post_trial_jobs[0].job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=10.0,
            )
        }

        post_trial_results = execute_post_trial_analysis(
            post_trial_jobs,
            context,
            build_workers=2,
            verify_workers=4,
        )

        assert len(post_trial_results) == 1
        assert post_trial_results[post_trial_jobs[0].job_id].success

    @patch("crsbench.experiment.build_orchestrator.DAGExecutor")
    @patch("crsbench.experiment.post_trial.DAGExecutor")
    def test_experiment_flow_bug_fixing_crs(
        self,
        mock_post_trial_executor_cls: MagicMock,
        mock_build_executor_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test flow with bug-fixing CRS that produces patches."""
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        # Create a patch file
        patch_path = tmp_path / "cpv_0_patch.diff"
        patch_path.write_text("--- a/file\n+++ b/file\n")

        # Create trial output structure
        trial_output_dir = tmp_path / "trial1"
        pov_dir = trial_output_dir / "output" / "povs"
        pov_dir.mkdir(parents=True)
        (pov_dir / "pov_0.bin").write_bytes(b"crash_input")

        # --- Phase 1: Upfront Builds ---
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=benchmark_path),
                sanitizer="address",
            ),
        ]

        build_jobs = create_upfront_build_jobs(trials)
        assert len(build_jobs) == 1

        # Mock build execution
        mock_build_executor = MagicMock()
        mock_build_executor_cls.return_value = mock_build_executor

        def mock_build_execute(
            jobs: list[Any], context: JobContext
        ) -> dict[str, ExecutorResult]:
            for job in jobs:
                context.shared[job.job_id] = {
                    "adapter": MagicMock(),
                    "build_results": {},
                }
            return {
                jobs[0].job_id: ExecutorResult(
                    job_id=jobs[0].job_id,
                    status=JobStatus.SUCCESS,
                    elapsed_seconds=25.0,
                )
            }

        mock_build_executor.execute.side_effect = mock_build_execute

        build_results, context = execute_upfront_builds(build_jobs)
        assert len(build_results) == 1

        # --- Phase 2: Bug-fixing trial result ---
        trial_results = [
            TrialResult(
                trial_id="trial-1",
                benchmark_path=benchmark_path,
                harness_name="fuzz_target",
                trial_output_dir=trial_output_dir,
                success=True,
                crs_type="bug_fixing",
                cpvs_found=["cpv_0"],
                patches=[patch_path],
            ),
        ]

        # --- Phase 3: Post-Trial Analysis ---
        build_job_ids = {job.benchmark_name: job.job_id for job in build_jobs}

        post_trial_jobs = create_post_trial_jobs(
            trial_results,
            build_job_ids,
            coverage_enabled=True,
        )

        # Should have: 1 coverage + 1 patch build + 1 patch test = 3 jobs
        assert len(post_trial_jobs) == 3

        coverage_jobs = [
            j for j in post_trial_jobs if isinstance(j, FlatCollectCoverageJob)
        ]
        build_patch_jobs = [
            j for j in post_trial_jobs if isinstance(j, BuildPatchVariantJob)
        ]
        test_patch_jobs = [
            j for j in post_trial_jobs if isinstance(j, PatchVariantTestJob)
        ]

        assert len(coverage_jobs) == 1
        assert len(build_patch_jobs) == 1
        assert len(test_patch_jobs) == 1

        # Verify dependency chain
        test_job = test_patch_jobs[0]
        build_job = build_patch_jobs[0]
        assert test_job.build_patch_job_id == build_job.job_id

        # Mock execution
        mock_post_trial_executor = MagicMock()
        mock_post_trial_executor_cls.return_value = mock_post_trial_executor
        mock_post_trial_executor.execute.return_value = {
            j.job_id: ExecutorResult(
                job_id=j.job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            )
            for j in post_trial_jobs
        }

        results = execute_post_trial_analysis(post_trial_jobs, context)
        assert len(results) == 3
        assert all(r.success for r in results.values())


class TestExperimentFlowEmptyTrials:
    """Test handling of empty trial lists."""

    def test_empty_trials_no_build_jobs(self) -> None:
        """Empty trial list creates no build jobs."""
        build_jobs = create_upfront_build_jobs([])
        assert build_jobs == []

    @patch("crsbench.experiment.build_orchestrator.DAGExecutor")
    def test_empty_builds_empty_results(
        self,
        mock_executor_cls: MagicMock,
    ) -> None:
        """Empty build jobs returns empty results."""
        results, context = execute_upfront_builds([])
        assert results == {}
        assert context.shared == {}
        # Executor should not be created for empty jobs
        mock_executor_cls.assert_not_called()

    def test_empty_trial_results_no_post_trial_jobs(self) -> None:
        """Empty trial results creates no post-trial jobs."""
        jobs = create_post_trial_jobs([], {})
        assert jobs == []

    def test_empty_post_trial_jobs_empty_results(self) -> None:
        """Empty post-trial jobs returns empty results."""
        context = JobContext()
        results = execute_post_trial_analysis([], context)
        assert results == {}


class TestExperimentModuleExports:
    """Test that all required functions are exported."""

    def test_all_functions_importable(self) -> None:
        """All required functions can be imported from crsbench.experiment."""
        from crsbench.experiment import (
            CRSRunJob,
            TrialResult,
            create_post_trial_jobs,
            create_upfront_build_jobs,
            execute_post_trial_analysis,
            execute_upfront_builds,
        )

        # Verify they're callable/classes
        assert callable(create_upfront_build_jobs)
        assert callable(execute_upfront_builds)
        assert callable(create_post_trial_jobs)
        assert callable(execute_post_trial_analysis)
        assert isinstance(CRSRunJob, type)
        assert isinstance(TrialResult, type)


class TestJobTypesAndDependencies:
    """Test job types and dependency relationships."""

    def test_build_jobs_have_correct_type(self, tmp_path: Path) -> None:
        """BuildVariantsJob has job_type 'build'."""
        trials = [
            MockTrial(
                benchmark_harness=MockBenchmarkHarness(path=tmp_path / "bench"),
                sanitizer="address",
            ),
        ]

        jobs = create_upfront_build_jobs(trials)
        assert len(jobs) == 1
        assert jobs[0].job_type == "build"

    def test_coverage_jobs_have_correct_type(self, tmp_path: Path) -> None:
        """FlatCollectCoverageJob has job_type 'verify'."""
        result = TrialResult(
            trial_id="trial-1",
            benchmark_path=tmp_path / "bench",
            harness_name="harness",
            trial_output_dir=tmp_path / "out",
            success=True,
            crs_type="bug_finding",
        )

        jobs = create_post_trial_jobs([result], {})
        assert len(jobs) == 1
        assert jobs[0].job_type == "verify"

    def test_crs_run_job_has_correct_type(self, tmp_path: Path) -> None:
        """CRSRunJob has job_type 'crs_run'."""
        job = CRSRunJob(
            crs_config_name="test-crs",
            benchmark_path=tmp_path / "bench",
            harness_name="harness",
            trial_num=1,
            trial_output_dir=tmp_path / "out",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=tmp_path / "registry",
            benchmarks_root=tmp_path / "benchmarks",
            crs_configs_dir=tmp_path / "configs",
        )
        assert job.job_type == "crs_run"
