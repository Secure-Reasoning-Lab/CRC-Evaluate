"""Full mode POV check job executor.

Uses OSSFuzzBuilder for building and OSSFuzzInfrastructure for reproduce.
"""

from crsbench.benchmark_ci.jobs.base import JobExecutor, logger
from crsbench.benchmark_ci.models import JobContext, JobResult
from crsbench.builder.types import VariantType


class FullBasePovCheckJob(JobExecutor):
    """Check full mode base commit: build and verify POVs crash.

    In full mode, base_commit is the vulnerable version.
    POVs SHOULD trigger crashes at base commit (vulnerability present).
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,
    ) -> JobResult:
        """Execute full base POV check.

        Args:
            job: Job context
            use_inc_build: Use incremental build image

        Steps:
        1. Build FULL_BASE variant using OSSFuzzBuilder
        2. Reproduce POVs and verify they DO crash
        """
        if not job.task:
            return JobResult(
                success=False, error_message="FULL_BASE_POV_CHECK requires task"
            )

        try:
            logger.info("=== FULL BASE POV CHECK ===")

            base_commit = job.task.base_commit
            logger.info(f"Building FULL_BASE variant at commit {base_commit[:8]}")

            # Step 1: Build FULL_BASE variant using OSSFuzzBuilder
            build_result = self._build_variant(
                benchmark=job.benchmark,
                variant_type=VariantType.FULL_BASE,
                commit=base_commit,
                sanitizer=job.sanitizer,
                use_inc_build=use_inc_build,
            )

            if not build_result.success:
                error_msg = (
                    str(build_result.error) if build_result.error else "Build failed"
                )
                return JobResult(success=False, error_message=error_msg)

            logger.info("Step 2/2: Verifying POVs DO crash (vulnerable version)")

            # Step 2: Reproduce POVs and verify they DO crash
            infra = self._get_infra()
            adapter = self._get_adapter(job.benchmark)
            variant_name = build_result.config.variant_name

            non_crashing_povs = []
            total_povs = 0

            for harness_name in adapter.get_harness_names():
                for vuln_keyword, pov in adapter.get_all_povs(harness_name):
                    pov_path = adapter.get_pov_path(harness_name, vuln_keyword, pov.id)
                    if not pov_path or not pov_path.exists():
                        logger.warning(f"POV file not found: {pov_path}")
                        continue

                    total_povs += 1
                    pov_data = pov_path.read_bytes()

                    output = infra.reproduce(
                        project_name=variant_name,
                        harness=harness_name,
                        pov_data=pov_data,
                        timeout=120,
                        pov_id=pov.id,
                    )

                    if not output.crashed:
                        non_crashing_povs.append(pov.id)
                        logger.error(f"  POV {pov.id} did NOT crash at base commit")

            if non_crashing_povs:
                return JobResult(
                    success=False,
                    error_message=f"POVs did not crash at base commit: {non_crashing_povs}",
                )

            logger.info(f"Verified {total_povs} POVs trigger crashes")
            return JobResult(success=True)

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))
