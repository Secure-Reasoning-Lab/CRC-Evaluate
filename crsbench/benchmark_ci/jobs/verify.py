"""Verification jobs for benchmark CI.

VerifyPovJob: Verify a POV against a variant
VerifyPatchJob: Verify a patch fixes all POVs for a CPV
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.utils import strip_ansi


@dataclass
class VerifyPovJob(Job):
    """Verify a POV against a variant.

    Checks whether a POV crashes (or doesn't crash) on a specific variant.
    The expected_crash flag determines what constitutes success.

    Attributes:
        benchmark: Benchmark name
        sanitizer: Sanitizer type
        variant_type: Variant type (deltabase, deltaref, cpv0, etc.)
        pov_id: POV identifier
        pov_path: Path to POV file
        harness: Harness name for reproduce
        expected_crash: Whether POV should crash on this variant
    """

    benchmark: str
    sanitizer: str
    variant_type: str
    pov_id: str
    pov_path: Path
    harness: str
    expected_crash: bool

    @property
    def job_id(self) -> str:
        """Unique job ID: verify-pov:{benchmark}-{sanitizer}-{variant}:{pov_id}."""
        return f"verify-pov:{self.benchmark}-{self.sanitizer}-{self.variant_type}:{self.pov_id}"

    @property
    def job_type(self) -> str:
        return "verify-pov"

    @property
    def depends_on(self) -> list[str]:
        """Depends on the build job for this variant."""
        return [f"build:{self.benchmark}-{self.sanitizer}-{self.variant_type}"]

    @property
    def variant_name(self) -> str:
        """Full variant name for reproduce."""
        return f"{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    def execute(self, context: JobContext) -> JobResult:
        """Execute the POV verification job.

        Uses OSSFuzzInfrastructure.reproduce() to test the POV.

        Args:
            context: Job context with infrastructure

        Returns:
            JobResult with verification status
        """
        started_at = datetime.now()

        try:
            # Read POV data
            pov_data = self.pov_path.read_bytes()

            # Run reproduce
            output = context.infra.reproduce(
                project_name=self.variant_name,
                harness=self.harness,
                pov_data=pov_data,
                timeout=context.timeout,
                pov_id=self.pov_id,
            )

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            actual_crash = output.crashed
            success = actual_crash == self.expected_crash

            # Determine error message if failed
            error = None
            if not success:
                error = self._get_error_message(actual_crash)

            # Clean logs
            logs = strip_ansi(output.stdout) if output.crashed else ""

            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                logs=logs,
                error=error,
                details={
                    "expected_crash": self.expected_crash,
                    "actual_crash": actual_crash,
                    "variant": self.variant_name,
                    "pov_id": self.pov_id,
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

    def _get_error_message(self, actual_crash: bool) -> str:
        """Generate error message for verification failure."""
        if self.expected_crash and not actual_crash:
            return f"POV {self.pov_id} did NOT crash on {self.variant_type} (expected crash)"

        if not self.expected_crash and actual_crash:
            if "deltabase" in self.variant_type:
                return f"ZERODAY: POV {self.pov_id} crashed on deltabase (pre-vulnerability)"
            if "allpatched" in self.variant_type:
                return f"UNINTENDED: POV {self.pov_id} crashed on allpatched (should be fixed)"
            return (
                f"POV {self.pov_id} crashed on {self.variant_type} (expected no crash)"
            )

        return ""


@dataclass
class VerifyPatchJob(Job):
    """Verify a patch fixes all POVs for its CPV.

    A patch for CPV_N must prevent ALL POVs targeting that CPV from crashing.

    Attributes:
        benchmark: Benchmark name
        sanitizer: Sanitizer type
        cpv_num: CPV number this patch fixes
        patch_path: Path to patch file
        povs_for_cpv: List of (pov_id, pov_path) tuples for this CPV
        harness: Harness name for reproduce
    """

    benchmark: str
    sanitizer: str
    cpv_num: int
    patch_path: Path
    povs_for_cpv: list[tuple[str, Path]] = field(default_factory=list)
    harness: str = ""

    @property
    def job_id(self) -> str:
        """Unique job ID: verify-patch:{benchmark}-{sanitizer}-cpv{N}."""
        return f"verify-patch:{self.benchmark}-{self.sanitizer}-cpv{self.cpv_num}"

    @property
    def job_type(self) -> str:
        return "verify-patch"

    @property
    def depends_on(self) -> list[str]:
        """Depends on the patched variant build."""
        return [f"build:{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"]

    @property
    def variant_name(self) -> str:
        """Full variant name for reproduce."""
        return f"{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"

    def execute(self, context: JobContext) -> JobResult:
        """Execute the patch verification job.

        Tests all POVs for this CPV against the patched variant.
        All POVs must NOT crash for the patch to be considered valid.

        Args:
            context: Job context with infrastructure

        Returns:
            JobResult with patch verification status
        """
        started_at = datetime.now()

        try:
            failed_povs = []
            passed_povs = []

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
