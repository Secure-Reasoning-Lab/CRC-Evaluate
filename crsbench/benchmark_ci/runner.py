"""Project CI runner for executing benchmark validation jobs.

Two-phase execution model:
1. Build Phase: Execute all BuildJobs (parallel via OSSFuzzBuilder)
2. Verify Phase: Execute all VerifyJobs (parallel via ThreadPoolExecutor)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from crsbench.benchmark_ci.jobs import BuildJob, Job, JobContext, JobResult
from crsbench.builder import OSSFuzzBuilder
from crsbench.builder.types import BuildResult
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProjectCIResult:
    """Complete CI result for a project.

    Collects all job results and provides summary statistics.

    Attributes:
        started_at: When CI started
        finished_at: When CI finished
        results: List of all job results
    """

    started_at: datetime
    finished_at: datetime
    results: list[JobResult] = field(default_factory=list)

    @property
    def build_results(self) -> list[JobResult]:
        """Get all build job results."""
        return [r for r in self.results if r.job_type == "build"]

    @property
    def verify_results(self) -> list[JobResult]:
        """Get all verification job results."""
        return [r for r in self.results if r.job_type.startswith("verify")]

    @property
    def total_build_time(self) -> float:
        """Total time spent on build jobs."""
        return sum(r.elapsed_seconds for r in self.build_results)

    @property
    def total_verify_time(self) -> float:
        """Total time spent on verification jobs."""
        return sum(r.elapsed_seconds for r in self.verify_results)

    @property
    def passed(self) -> bool:
        """True if all jobs passed."""
        return all(r.success for r in self.results)

    @property
    def passed_count(self) -> int:
        """Number of jobs that passed."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        """Number of jobs that failed."""
        return sum(1 for r in self.results if not r.success)

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics."""
        return {
            "total_jobs": len(self.results),
            "passed": self.passed_count,
            "failed": self.failed_count,
            "build_time_seconds": self.total_build_time,
            "verify_time_seconds": self.total_verify_time,
            "duration_seconds": (self.finished_at - self.started_at).total_seconds(),
        }

    def get_failed_jobs(self) -> list[JobResult]:
        """Get all failed job results."""
        return [r for r in self.results if not r.success]

    def to_csv_rows(self) -> list[dict[str, Any]]:
        """Convert results to CSV-compatible rows."""
        rows = []
        for r in self.results:
            row = {
                "job_id": r.job_id,
                "job_type": r.job_type,
                "success": r.success,
                "elapsed_seconds": r.elapsed_seconds,
                "error": r.error or "",
                "started_at": r.started_at.isoformat() if r.started_at else "",
                "finished_at": r.finished_at.isoformat() if r.finished_at else "",
            }
            # Add details as separate columns
            for key, value in r.details.items():
                row[f"detail_{key}"] = value
            rows.append(row)
        return rows


class ProjectCIRunner:
    """Runs CI for a single project using two-phase execution.

    Phase 1 (Build): Builds all variants in parallel
    Phase 2 (Verify): Runs all verification jobs in parallel

    This separation eliminates race conditions and simplifies coordination.

    Attributes:
        builder: OSSFuzzBuilder for building variants
        infra: OSSFuzzInfrastructure for reproduce operations
        verify_workers: Number of parallel verification workers
        timeout: Timeout for verification jobs in seconds
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        *,
        build_workers: int = 4,
        verify_workers: int = 4,
        timeout: int = 120,
    ):
        """Initialize the runner.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz directory
            build_workers: Number of parallel build workers
            verify_workers: Number of parallel verification workers
            timeout: Timeout for verification jobs in seconds
        """
        self.builder = OSSFuzzBuilder(oss_fuzz_path, max_workers=build_workers)
        self.infra = self.builder.infra
        self.verify_workers = verify_workers
        self.timeout = timeout

    def run(self, jobs: list[Job]) -> ProjectCIResult:
        """Execute jobs in two phases.

        Args:
            jobs: List of jobs to execute (build and verify)

        Returns:
            ProjectCIResult with all job results
        """
        started_at = datetime.now()

        # Separate jobs by type
        build_jobs = [j for j in jobs if isinstance(j, BuildJob)]
        verify_jobs = [j for j in jobs if j.job_type.startswith("verify")]

        results: dict[str, JobResult] = {}

        # Phase 1: Build all variants
        logger.debug(f"=== Build Phase: {len(build_jobs)} jobs ===")
        build_results = self._execute_build_phase(build_jobs)
        results.update(build_results)

        # Check for build failures
        failed_builds = {jid for jid, r in build_results.items() if not r.success}
        if failed_builds:
            logger.warning(f"Build failures: {len(failed_builds)} jobs failed")

        # Phase 2: Verify (skip jobs with failed dependencies)
        logger.debug(f"=== Verify Phase: {len(verify_jobs)} jobs ===")
        verify_results = self._execute_verify_phase(verify_jobs, failed_builds)
        results.update(verify_results)

        finished_at = datetime.now()

        return ProjectCIResult(
            started_at=started_at,
            finished_at=finished_at,
            results=list(results.values()),
        )

    def _execute_build_phase(self, jobs: list[BuildJob]) -> dict[str, JobResult]:
        """Execute build jobs using OSSFuzzBuilder.

        Extracts BuildConfigs and uses builder's parallel execution.

        Args:
            jobs: List of BuildJob instances

        Returns:
            Dictionary mapping job_id to JobResult
        """
        results = {}

        if not jobs:
            return results

        # Extract BuildConfigs and use builder's parallel execution
        configs = [job.config for job in jobs]

        # Create a mapping from variant_name to job for result correlation
        variant_to_job = {job.config.variant_name: job for job in jobs}

        # Use builder's batch build
        build_results = self.builder.build_variants(configs)

        # Convert BuildResults to JobResults
        for variant_name, br in build_results.items():
            job = variant_to_job.get(variant_name)
            if not job:
                continue

            result = self._build_result_to_job_result(job, br)
            results[job.job_id] = result

            status = "PASS" if result.success else "FAIL"
            cached = " (cached)" if br.cached else ""
            logger.debug(
                f"  [{job.job_id}] {status}{cached} ({result.elapsed_seconds:.1f}s)"
            )

        return results

    def _build_result_to_job_result(self, job: BuildJob, br: BuildResult) -> JobResult:
        """Convert BuildResult to JobResult.

        Args:
            job: The BuildJob
            br: BuildResult from builder

        Returns:
            JobResult with build information
        """
        return JobResult(
            job_id=job.job_id,
            job_type="build",
            success=br.success,
            started_at=datetime.now(),  # Approximation - builder doesn't track per-job timing
            finished_at=datetime.now(),
            elapsed_seconds=br.elapsed_seconds,
            error=br.error,
            artifacts={"build_path": br.build_path} if br.build_path else {},
            details={
                "variant_name": br.variant_name,
                "cached": br.cached,
            },
        )

    def _execute_verify_phase(
        self,
        jobs: list[Job],
        failed_builds: set[str],
    ) -> dict[str, JobResult]:
        """Execute verify jobs in parallel.

        Jobs with failed dependencies are skipped.

        Args:
            jobs: List of verification jobs
            failed_builds: Set of failed build job IDs

        Returns:
            Dictionary mapping job_id to JobResult
        """
        results = {}

        if not jobs:
            return results

        context = JobContext(
            builder=self.builder,
            infra=self.infra,
            timeout=self.timeout,
        )

        with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
            futures = {}

            for job in jobs:
                # Skip if any dependency failed
                deps_failed = any(dep in failed_builds for dep in job.depends_on)
                if deps_failed:
                    results[job.job_id] = JobResult(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        success=False,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                        elapsed_seconds=0,
                        error="Dependency build failed",
                    )
                    logger.warning(f"  [{job.job_id}] SKIP (dependency failed)")
                else:
                    futures[executor.submit(job.execute, context)] = job

            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                    results[job.job_id] = result
                    status = "PASS" if result.success else "FAIL"
                    logger.debug(
                        f"  [{job.job_id}] {status} ({result.elapsed_seconds:.1f}s)"
                    )
                except Exception as e:
                    results[job.job_id] = JobResult(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        success=False,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                        elapsed_seconds=0,
                        error=str(e),
                    )
                    logger.error(f"  [{job.job_id}] ERROR: {e}")

        return results

    def run_dry(self, jobs: list[Job]) -> None:
        """Print job execution plan without running.

        Args:
            jobs: List of jobs to plan
        """
        build_jobs = [j for j in jobs if j.job_type == "build"]
        verify_jobs = [j for j in jobs if j.job_type.startswith("verify")]

        logger.debug("=== Dry Run: Job Execution Plan ===")
        logger.debug("")
        logger.debug(f"Build Phase ({len(build_jobs)} jobs):")
        for job in build_jobs:
            logger.debug(f"  - {job.job_id}")

        logger.debug("")
        logger.debug(f"Verify Phase ({len(verify_jobs)} jobs):")
        for job in verify_jobs:
            deps = ", ".join(job.depends_on) if job.depends_on else "none"
            logger.debug(f"  - {job.job_id}")
            logger.debug(f"    depends_on: {deps}")

        logger.debug("")
        logger.debug(f"Total: {len(jobs)} jobs")
