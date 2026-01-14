"""Benchmark CI runner for orchestrating test jobs.

This module provides the main runner class for executing benchmark CI tests.
Follows the pattern from crsbench.evaluation.runner.BenchmarkRunner.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from crsbench.benchmark_ci.jobs import (
    CoverageCheckJob,
    DeltaBasePovCheckJob,
    DeltaRefPovCheckJob,
    FullBasePovCheckJob,
    IncBuildPullJob,
    JobExecutor,
    PatchCheckJob,
)
from crsbench.benchmark_ci.models import (
    CIResult,
    ExecJobType,
    JobContext,
    JobResult,
    ResultCollector,
    Task,
    TaskMode,
    get_benchmarks_root,
)
from crsbench.builder.types import BenchmarkMode
from crsbench.utils.logger import get_logger
from crsbench.validation import validate_benchmark
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.schemas import ProjectConfig

logger = get_logger(__name__)

# Default job types (excludes coverage_check)
DEFAULT_JOB_TYPES: Set[ExecJobType] = {
    ExecJobType.DELTA_BASE_POV_CHECK,
    ExecJobType.DELTA_REF_POV_CHECK,
    ExecJobType.FULL_BASE_POV_CHECK,
    ExecJobType.PATCH_CHECK,
    ExecJobType.INC_BUILD_PULL,
}


class BenchmarkCIRunner:
    """Main class for running benchmark CI tests.

    This runner orchestrates the execution of various CI jobs
    (POV checks, patch checks, coverage checks, inc-build pull)
    for benchmarks.
    """

    def __init__(
        self,
        *,
        force_rebuild: bool = False,
        max_workers: int = 1,
    ):
        """Initialize BenchmarkCIRunner.

        Args:
            force_rebuild: Force rebuild Docker images even if they already exist
            max_workers: Maximum number of parallel workers (default: 1)
        """
        self.force_rebuild = force_rebuild
        self.max_workers = max(1, max_workers)
        self.result_collector = ResultCollector()

        # Initialize job executors
        self._executors: Dict[ExecJobType, JobExecutor] = {
            ExecJobType.DELTA_BASE_POV_CHECK: DeltaBasePovCheckJob(),
            ExecJobType.DELTA_REF_POV_CHECK: DeltaRefPovCheckJob(),
            ExecJobType.FULL_BASE_POV_CHECK: FullBasePovCheckJob(),
            ExecJobType.PATCH_CHECK: PatchCheckJob(),
            ExecJobType.COVERAGE_CHECK: CoverageCheckJob(),
            ExecJobType.INC_BUILD_PULL: IncBuildPullJob(),
        }

    def run(
        self,
        benchmarks: Set[str],
        job_types: Optional[Set[ExecJobType]] = None,
        *,
        check_default_only: bool = False,
    ) -> ResultCollector:
        """Run CI tests for benchmarks.

        Args:
            benchmarks: Set of benchmark names to test
            job_types: Optional set of job types to filter (None means default)
            check_default_only: Only test libfuzzer + address/none sanitizers

        Returns:
            ResultCollector with all test results
        """
        self.result_collector.start()

        # Use default job types if not specified
        effective_job_types = job_types if job_types is not None else DEFAULT_JOB_TYPES

        # Generate jobs
        jobs = self._generate_jobs(
            benchmarks, effective_job_types, check_default_only=check_default_only
        )

        logger.info(f"Generated {len(jobs)} jobs for {len(benchmarks)} benchmarks")

        # Execute jobs (parallel or sequential)
        if self.max_workers > 1:
            self._execute_jobs_parallel(jobs)
        else:
            self._execute_jobs_sequential(jobs)

        self.result_collector.finish()
        return self.result_collector

    def generate_jobs(
        self,
        benchmarks: Set[str],
        job_types: Optional[Set[ExecJobType]] = None,
        *,
        check_default_only: bool = False,
    ) -> List[JobContext]:
        """Generate jobs without executing them (for dry-run).

        Args:
            benchmarks: Set of benchmark names to test
            job_types: Optional set of job types to filter (None means default)
            check_default_only: Only test libfuzzer + address/none sanitizers

        Returns:
            List of JobContext objects that would be executed
        """
        effective_job_types = job_types if job_types is not None else DEFAULT_JOB_TYPES
        return self._generate_jobs(
            benchmarks, effective_job_types, check_default_only=check_default_only
        )

    def _execute_jobs_sequential(self, jobs: List[JobContext]) -> None:
        """Execute jobs sequentially."""
        for i, job in enumerate(jobs, 1):
            logger.info("-" * 80)
            logger.info(f"Job {i}/{len(jobs)}: {job}")

            start_time = datetime.now()
            result = self._execute_job(job)
            end_time = datetime.now()

            # Create and collect result
            ci_result = CIResult.from_job_result(job, result, start_time, end_time)
            self.result_collector.add_result(ci_result)

            if not result.success:
                logger.error(f"Job failed: {result.error_message}")

    def _execute_jobs_parallel(self, jobs: List[JobContext]) -> None:
        """Execute jobs in parallel using ThreadPoolExecutor.

        INC_BUILD_PULL jobs run first as pre-validation. If any fail,
        remaining jobs for that benchmark are skipped.
        """
        # Separate INC_BUILD_PULL jobs from others
        inc_build_jobs = [j for j in jobs if j.job_type == ExecJobType.INC_BUILD_PULL]
        other_jobs = [j for j in jobs if j.job_type != ExecJobType.INC_BUILD_PULL]

        total_jobs = len(jobs)
        completed = 0

        # Phase 1: Run INC_BUILD_PULL jobs first (pre-validation)
        failed_benchmarks: Set[str] = set()
        if inc_build_jobs:
            logger.info(
                f"Phase 1: Pulling inc-build images for {len(inc_build_jobs)} benchmarks..."
            )
            completed = self._run_job_batch(
                inc_build_jobs, total_jobs, completed, failed_benchmarks
            )

        # Phase 2: Run other jobs (skip benchmarks that failed inc-build pull)
        if other_jobs:
            if failed_benchmarks:
                # Filter out jobs for failed benchmarks
                skipped_jobs = [
                    j for j in other_jobs if j.benchmark in failed_benchmarks
                ]
                other_jobs = [
                    j for j in other_jobs if j.benchmark not in failed_benchmarks
                ]

                for job in skipped_jobs:
                    completed += 1
                    ci_result = CIResult.from_job_result(
                        job,
                        JobResult(
                            success=False,
                            error_message="Skipped: inc-build pull failed",
                        ),
                        datetime.now(),
                        datetime.now(),
                    )
                    self.result_collector.add_result(ci_result)
                    logger.warning(
                        f"[{completed}/{total_jobs}] {job.benchmark}: SKIPPED (inc-build pull failed)"
                    )

            if other_jobs:
                logger.info(
                    f"Phase 2: Running {len(other_jobs)} jobs with {self.max_workers} workers..."
                )
                self._run_job_batch(
                    other_jobs, total_jobs, completed, failed_benchmarks
                )

        # Summary
        summary = self.result_collector.get_summary()
        logger.info(
            f"Parallel execution complete: {summary['passed']}/{summary['total']} passed"
        )

    def _run_job_batch(
        self,
        jobs: List[JobContext],
        total_jobs: int,
        completed: int,
        failed_benchmarks: Set[str],
    ) -> int:
        """Run a batch of jobs in parallel.

        Args:
            jobs: Jobs to run
            total_jobs: Total number of jobs (for progress logging)
            completed: Number of jobs already completed
            failed_benchmarks: Set to add failed benchmark names to

        Returns:
            Updated completed count
        """
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_job = {
                executor.submit(self._execute_job_with_timing, job): job for job in jobs
            }

            for future in as_completed(future_to_job):
                job = future_to_job[future]
                completed += 1

                try:
                    result, start_time, end_time = future.result()

                    ci_result = CIResult.from_job_result(
                        job, result, start_time, end_time
                    )
                    self.result_collector.add_result(ci_result)

                    status = "OK" if result.success else "FAILED"
                    logger.info(f"[{completed}/{total_jobs}] {job.benchmark}: {status}")

                    if not result.success:
                        logger.error(f"  Error: {result.error_message}")
                        failed_benchmarks.add(job.benchmark)

                except Exception as e:
                    logger.error(
                        f"[{completed}/{total_jobs}] {job.benchmark}: Unexpected error: {e}"
                    )
                    ci_result = CIResult.from_job_result(
                        job,
                        JobResult(success=False, error_message=str(e)),
                        datetime.now(),
                        datetime.now(),
                    )
                    self.result_collector.add_result(ci_result)
                    failed_benchmarks.add(job.benchmark)

        return completed

    def _execute_job_with_timing(
        self, job: JobContext
    ) -> Tuple[JobResult, datetime, datetime]:
        """Execute a job and return result with timing info."""
        start_time = datetime.now()
        result = self._execute_job(job)
        end_time = datetime.now()
        return result, start_time, end_time

    def _execute_job(self, job: JobContext) -> JobResult:
        """Execute a single job."""
        executor = self._executors.get(job.job_type)
        if not executor:
            return JobResult(
                success=False, error_message=f"Unknown job type: {job.job_type}"
            )

        return executor.execute(job, use_inc_build=job.use_inc_build)

    def _generate_jobs(
        self,
        benchmarks: Set[str],
        job_types: Optional[Set[ExecJobType]] = None,
        *,
        check_default_only: bool = False,
    ) -> List[JobContext]:
        """Generate test jobs for benchmarks."""
        jobs: List[JobContext] = []

        for benchmark in sorted(benchmarks):
            try:
                benchmark_jobs = self._generate_benchmark_jobs(
                    benchmark, job_types, check_default_only=check_default_only
                )
                jobs.extend(benchmark_jobs)
            except Exception as e:
                logger.error(f"Failed to generate jobs for {benchmark}: {e}")
                self.result_collector.add_skipped(benchmark, str(e))

        return sorted(jobs)

    def _generate_benchmark_jobs(
        self,
        benchmark: str,
        job_types: Optional[Set[ExecJobType]] = None,
        *,
        check_default_only: bool = False,
    ) -> List[JobContext]:
        """Generate jobs for a single benchmark."""
        jobs: List[JobContext] = []

        # Validate benchmark first
        benchmark_path = Path(get_benchmarks_root()) / benchmark
        result = validate_benchmark(benchmark_path)
        if not result.is_valid:
            raise ValueError(f"Invalid benchmark: {result.errors}")

        # Load configs
        project_config = self._load_project_config(benchmark)
        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if not adapter:
            raise ValueError(f"Failed to load MetaYamlAdapter for {benchmark}")

        language = project_config.language
        sanitizers = project_config.sanitizers
        fuzzing_engines = project_config.fuzzing_engines
        use_inc_build = project_config.inc_build

        # Get tasks from adapter
        tasks = self._get_tasks_from_adapter(adapter)

        # Check if delta mode exists (to skip redundant full_base_pov_check)
        has_delta_mode = any(t.mode == TaskMode.DELTA for t in tasks)

        # Find the delta task for patch checks (prefer delta over full)
        delta_task = next((t for t in tasks if t.mode == TaskMode.DELTA), None)
        patch_task = delta_task or (tasks[0] if tasks else None)

        # Generate POV check jobs for each task
        for task in tasks:
            for engine in fuzzing_engines:
                for sanitizer in sanitizers:
                    if check_default_only:
                        if sanitizer not in ["none", "address"]:
                            continue
                        if engine != "libfuzzer":
                            continue

                    # Delta mode POV checks
                    if task.mode == TaskMode.DELTA:
                        if (
                            not job_types
                            or ExecJobType.DELTA_BASE_POV_CHECK in job_types
                        ):
                            jobs.append(
                                JobContext(
                                    job_type=ExecJobType.DELTA_BASE_POV_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                    use_inc_build=use_inc_build,
                                )
                            )

                        if (
                            not job_types
                            or ExecJobType.DELTA_REF_POV_CHECK in job_types
                        ):
                            jobs.append(
                                JobContext(
                                    job_type=ExecJobType.DELTA_REF_POV_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                    use_inc_build=use_inc_build,
                                )
                            )

                    # Full mode POV checks (skip if delta mode exists)
                    if task.mode == TaskMode.FULL and not has_delta_mode:
                        if (
                            not job_types
                            or ExecJobType.FULL_BASE_POV_CHECK in job_types
                        ):
                            jobs.append(
                                JobContext(
                                    job_type=ExecJobType.FULL_BASE_POV_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                    use_inc_build=use_inc_build,
                                )
                            )

        # Generate patch checks using adapter
        if patch_task and (not job_types or ExecJobType.PATCH_CHECK in job_types):
            for engine in fuzzing_engines:
                for sanitizer in sanitizers:
                    if check_default_only:
                        if sanitizer not in ["none", "address"]:
                            continue
                        if engine != "libfuzzer":
                            continue

                    for harness_name in adapter.get_harness_names():
                        for vuln_keyword, pov in adapter.get_all_povs(harness_name):
                            patch_path = adapter.get_patch_path(
                                harness_name, vuln_keyword
                            )
                            pov_path = adapter.get_pov_path(
                                harness_name, vuln_keyword, pov.id
                            )

                            if (
                                pov.sanitizer == sanitizer
                                and engine == "libfuzzer"
                                and patch_path
                            ):
                                jobs.append(
                                    JobContext(
                                        job_type=ExecJobType.PATCH_CHECK,
                                        task=patch_task,
                                        benchmark=benchmark,
                                        language=language,
                                        engine=engine,
                                        sanitizer=sanitizer,
                                        harness_name=harness_name,
                                        vuln_keyword=vuln_keyword,
                                        pov_id=pov.id,
                                        patch_path=patch_path,
                                        pov_path=pov_path,
                                        use_inc_build=use_inc_build,
                                    )
                                )

        # Add coverage check (once per benchmark)
        if not job_types or ExecJobType.COVERAGE_CHECK in job_types:
            jobs.append(
                JobContext(
                    job_type=ExecJobType.COVERAGE_CHECK,
                    task=tasks[0] if tasks else None,
                    benchmark=benchmark,
                    language=language,
                    engine="libfuzzer",
                    sanitizer="coverage",
                )
            )

        # Pull inc-build image (once per benchmark, if inc_build enabled)
        if project_config.inc_build:
            if not job_types or ExecJobType.INC_BUILD_PULL in job_types:
                jobs.append(
                    JobContext(
                        job_type=ExecJobType.INC_BUILD_PULL,
                        task=tasks[0] if tasks else None,
                        benchmark=benchmark,
                        language=language,
                        engine="libfuzzer",
                        sanitizer="address",
                    )
                )

        return jobs

    def _get_tasks_from_adapter(self, adapter: MetaYamlAdapter) -> List[Task]:
        """Get tasks (delta/full mode) from MetaYamlAdapter."""
        tasks = []

        mode = adapter.get_mode()
        if mode == BenchmarkMode.DELTA:
            ref_commit = adapter.get_ref_commit()
            if ref_commit:
                tasks.append(
                    Task(
                        mode=TaskMode.DELTA,
                        base_commit=adapter.get_base_commit(),
                        ref_commit=ref_commit,
                    )
                )
        else:
            tasks.append(
                Task(mode=TaskMode.FULL, base_commit=adapter.get_base_commit())
            )

        return tasks

    def _load_project_config(self, benchmark: str) -> ProjectConfig:
        """Load project configuration from project.yaml."""
        benchmark_path = Path(get_benchmarks_root()) / benchmark
        project_yaml = benchmark_path / "project.yaml"

        if not project_yaml.exists():
            raise ValueError(f"project.yaml not found for {benchmark}")

        with project_yaml.open() as f:
            data = yaml.safe_load(f) or {}

        return ProjectConfig(**data)
