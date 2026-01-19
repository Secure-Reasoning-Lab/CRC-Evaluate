"""Build job for benchmark CI.

BuildJob builds a single variant and tracks timing, logs, and artifacts.
"""

from dataclasses import dataclass
from datetime import datetime

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.builder.types import BuildConfig


@dataclass
class BuildJob(Job):
    """Build a variant - tracked with time, logs, artifacts.

    Wraps OSSFuzzBuilder.build_single() with job tracking.

    Attributes:
        benchmark: Benchmark name (e.g., "curl")
        sanitizer: Sanitizer type (e.g., "address")
        variant_type: Variant type (e.g., "deltabase", "cpv0")
        config: BuildConfig for the variant
    """

    benchmark: str
    sanitizer: str
    variant_type: str
    config: BuildConfig

    @property
    def job_id(self) -> str:
        """Unique job ID: build:{benchmark}-{sanitizer}-{variant_type}."""
        return f"build:{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    @property
    def job_type(self) -> str:
        return "build"

    @property
    def variant_name(self) -> str:
        """Full variant name for build output."""
        return f"{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    def execute(self, context: JobContext) -> JobResult:
        """Execute the build job.

        Note: This method is provided for single-job execution and testing.
        ProjectCIRunner uses builder.build_variants() for batch execution,
        which is more efficient for building multiple variants in parallel.

        Uses OSSFuzzBuilder.build_single() which handles:
        - Source preparation (pkgs/ or git clone)
        - Patch application
        - Docker build
        - Build caching

        Args:
            context: Job context with builder

        Returns:
            JobResult with build status and timing
        """
        started_at = datetime.now()

        try:
            # Use existing builder - handles all complexity
            result = context.builder.build_single(self.config)

            finished_at = datetime.now()
            elapsed = (
                result.elapsed_seconds or (finished_at - started_at).total_seconds()
            )

            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=result.success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=result.error,
                artifacts=(
                    {"build_path": result.build_path} if result.build_path else {}
                ),
                details={
                    "variant_name": result.variant_name,
                    "cached": result.cached,
                },
            )

        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
