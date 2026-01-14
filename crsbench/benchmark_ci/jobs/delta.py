"""Delta mode POV check job executors.

Uses OSSFuzzBuilder for building and OSSFuzzInfrastructure for reproduce.
"""

from crsbench.benchmark_ci.jobs.base import JobExecutor, logger
from crsbench.benchmark_ci.models import JobContext, JobResult
from crsbench.builder.types import VariantType


class DeltaBasePovCheckJob(JobExecutor):
    """Check delta mode base commit: build and verify POVs do NOT crash.

    In delta mode, base_commit is the clean version before bug-inducing diff.
    POVs should NOT trigger crashes at base commit (no vulnerability yet).
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,
    ) -> JobResult:
        """Execute delta base POV check.

        Args:
            job: Job context
            use_inc_build: Use incremental build image

        Steps:
        1. Build DELTA_BASE variant using OSSFuzzBuilder
        2. Reproduce POVs and verify they do NOT crash
        """
        if not job.task:
            return JobResult(
                success=False, error_message="DELTA_BASE_POV_CHECK requires task"
            )

        try:
            logger.info("=== DELTA BASE POV CHECK ===")

            base_commit = job.task.base_commit
            logger.info(f"Building DELTA_BASE variant at commit {base_commit[:8]}")

            # Step 1: Build DELTA_BASE variant using OSSFuzzBuilder
            build_result = self._build_variant(
                benchmark=job.benchmark,
                variant_type=VariantType.DELTA_BASE,
                commit=base_commit,
                sanitizer=job.sanitizer,
                use_inc_build=use_inc_build,
            )

            if not build_result.success:
                error_msg = (
                    str(build_result.error) if build_result.error else "Build failed"
                )
                return JobResult(success=False, error_message=error_msg)

            logger.info("Step 2/2: Verifying POVs do NOT crash (clean version)")

            # Step 2: Reproduce POVs and verify they do NOT crash
            infra = self._get_infra()
            adapter = self._get_adapter(job.benchmark)
            variant_name = build_result.config.variant_name

            failed_povs = []
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

                    if output.crashed:
                        failed_povs.append(pov.id)
                        logger.error(
                            f"  POV {pov.id} unexpectedly crashed at base commit"
                        )

            if failed_povs:
                return JobResult(
                    success=False,
                    error_message=f"POVs crashed at base commit: {failed_povs}",
                )

            logger.info(f"Verified {total_povs} POVs do NOT crash (clean version)")
            return JobResult(success=True)

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))


class DeltaRefPovCheckJob(JobExecutor):
    """Check delta mode ref commit: build and verify POVs crash.

    In delta mode, ref_commit is the vulnerable version after bug-inducing diff.
    POVs SHOULD trigger crashes at ref commit (vulnerability present).
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,
    ) -> JobResult:
        """Execute delta ref POV check.

        Args:
            job: Job context
            use_inc_build: Use incremental build image

        Steps:
        1. Build DELTA_REF variant using OSSFuzzBuilder
        2. Reproduce POVs and verify they DO crash
        """
        if not job.task:
            return JobResult(
                success=False, error_message="DELTA_REF_POV_CHECK requires task"
            )

        try:
            logger.info("=== DELTA REF POV CHECK ===")

            ref_commit = job.task.ref_commit
            if not ref_commit:
                return JobResult(
                    success=False,
                    error_message="No ref_commit found for delta mode task",
                )

            logger.info(f"Building DELTA_REF variant at commit {ref_commit[:8]}")

            # Step 1: Build DELTA_REF variant using OSSFuzzBuilder
            build_result = self._build_variant(
                benchmark=job.benchmark,
                variant_type=VariantType.DELTA_REF,
                commit=ref_commit,
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
                        logger.error(f"  POV {pov.id} did NOT crash at ref commit")

            if non_crashing_povs:
                return JobResult(
                    success=False,
                    error_message=f"POVs did not crash at ref commit: {non_crashing_povs}",
                )

            logger.info(
                f"Verified {total_povs} POVs trigger crashes (vulnerable version)"
            )
            return JobResult(success=True)

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))
