"""Patch check job executor.

Uses PatchVerificationEngine for complete patch verification.
"""

from crsbench.benchmark_ci.jobs.base import JobExecutor, logger
from crsbench.benchmark_ci.models import JobContext, JobResult
from crsbench.evaluation.verification.models import PatchInfo, PatchVerificationStatus


class PatchCheckJob(JobExecutor):
    """Check that patch fixes the vulnerability using PatchVerificationEngine.

    PatchVerificationEngine handles:
    - Source cloning at the appropriate commit
    - Patch application
    - Building (with inc-build support)
    - POV verification
    - Unit test execution
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,
    ) -> JobResult:
        """Execute patch check using PatchVerificationEngine.

        Args:
            job: Job context
            use_inc_build: Use incremental build image

        Steps:
        PatchVerificationEngine handles everything:
        1. Clone source at vulnerable commit
        2. Apply patch
        3. Build with inc-build or standard build
        4. Verify POV doesn't crash
        5. Run unit tests
        """
        if (
            not job.task
            or not job.patch_path
            or not job.pov_path
            or not job.harness_name
        ):
            return JobResult(
                success=False,
                error_message="PATCH_CHECK requires task, patch_path, pov_path, and harness_name",
            )

        try:
            logger.info("=== PATCH CHECK ===")
            logger.info(f"Verifying patch fixes {job.vuln_keyword}")

            # Create PatchVerificationEngine with appropriate settings
            engine = self._get_patch_engine(
                sanitizer=job.sanitizer,
                use_inc_build=use_inc_build,
            )

            try:
                benchmark_path = self._get_benchmark_path(job.benchmark)

                # Create PatchInfo
                patch_info = PatchInfo(
                    patch_id=f"patch_{job.vuln_keyword}",
                    pov_id=job.vuln_keyword or "",
                    patch_path=job.patch_path,
                )

                # Verify the patch - engine handles everything (clone, patch, build, test)
                result = engine.verify_patch(
                    benchmark_path=benchmark_path,
                    patch=patch_info,
                    harness=job.harness_name,
                    pov_path=job.pov_path,
                )

                if result.status == PatchVerificationStatus.VALID:
                    logger.info(f"Patch verified: {result.security_verdict}")
                    logger.info(f"  POV test passed: {result.pov_test_passed}")
                    logger.info(f"  Unit tests passed: {result.unit_tests_passed}")
                    return JobResult(success=True)

                error_msg = f"Patch verification failed: {result.status.value}"
                if result.details:
                    error_msg += f" - {result.details}"
                logger.error(error_msg)
                return JobResult(success=False, error_message=error_msg)

            finally:
                engine.cleanup()

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))
