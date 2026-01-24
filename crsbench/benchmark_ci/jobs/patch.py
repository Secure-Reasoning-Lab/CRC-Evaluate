"""Patch jobs for benchmark CI.

BuildPatchJob: Apply a patch and rebuild the variant.
TestPatchJob: Run POVs against a patched build to verify the fix.

These split the monolithic VerifyPatchJob into composable DAG nodes:
  BuildJob -> BuildPatchJob -> TestPatchJob
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.builder.types import BuildConfig


@dataclass
class BuildPatchJob(Job):
    """Apply a patch and rebuild the variant.

    Produces a patched variant that TestPatchJob can verify against.

    Attributes:
        benchmark: Benchmark name (e.g., "curl")
        sanitizer: Sanitizer type (e.g., "address")
        cpv_num: CPV number this patch targets
        patch_path: Path to the patch file
        config: BuildConfig with patch already baked in
    """

    benchmark: str
    sanitizer: str
    cpv_num: int
    patch_path: Path
    config: BuildConfig

    @property
    def job_id(self) -> str:
        return f"build-patch:{self.benchmark}-{self.sanitizer}-cpv{self.cpv_num}"

    @property
    def job_type(self) -> str:
        return "build-patch"

    @property
    def depends_on(self) -> list[str]:
        """Depends on the base build (deltaref) for inc-build."""
        return [f"build:{self.benchmark}-{self.sanitizer}-deltaref"]

    @property
    def variant_name(self) -> str:
        return f"{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"

    def execute(self, context: JobContext) -> JobResult:
        """Apply patch and rebuild the variant."""
        if not context.builder:
            raise RuntimeError("JobContext.builder is required for PatchBuildJob")

        started_at = datetime.now()

        try:
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
                    "cpv_num": self.cpv_num,
                    "patch_path": str(self.patch_path),
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


@dataclass
class TestPatchJob(Job):
    """Run POVs against a patched build to verify the fix.

    All POVs for the target CPV must NOT crash for the patch to be valid.

    Attributes:
        benchmark: Benchmark name
        sanitizer: Sanitizer type
        cpv_num: CPV number this patch fixes
        povs_for_cpv: List of (pov_id, pov_path) tuples
        harness: Harness name for reproduce
    """

    benchmark: str
    sanitizer: str
    cpv_num: int
    povs_for_cpv: list[tuple[str, Path]] = field(default_factory=list)
    harness: str = ""

    @property
    def job_id(self) -> str:
        return f"test-patch:{self.benchmark}-{self.sanitizer}-cpv{self.cpv_num}"

    @property
    def job_type(self) -> str:
        return "test-patch"

    @property
    def depends_on(self) -> list[str]:
        """Depends on the patched variant build."""
        return [f"build-patch:{self.benchmark}-{self.sanitizer}-cpv{self.cpv_num}"]

    @property
    def variant_name(self) -> str:
        return f"{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"

    def execute(self, context: JobContext) -> JobResult:
        """Run POVs against patched variant. None should crash."""
        if not context.infra:
            raise RuntimeError("JobContext.infra is required for PatchTestJob")

        started_at = datetime.now()

        try:
            failed_povs: list[str] = []
            passed_povs: list[str] = []

            for pov_id, pov_path in self.povs_for_cpv:
                pov_data = pov_path.read_bytes()

                output = context.infra.reproduce(
                    project_name=self.variant_name,
                    harness=self.harness,
                    pov_data=pov_data,
                    timeout=context.timeout,
                    pov_id=pov_id,
                )

                if output.crashed:
                    failed_povs.append(pov_id)
                else:
                    passed_povs.append(pov_id)

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            success = len(failed_povs) == 0
            error = None
            if failed_povs:
                error = f"Patch does not fix POVs: {failed_povs}"

            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=error,
                details={
                    "cpv_num": self.cpv_num,
                    "total_povs": len(self.povs_for_cpv),
                    "fixed": len(passed_povs),
                    "failed": failed_povs,
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
