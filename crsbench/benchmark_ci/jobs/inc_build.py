"""Incremental build image pull job executor.

Pulls inc-build image from registry for faster incremental builds.
"""

from crsbench.benchmark_ci.jobs.base import JobExecutor, logger
from crsbench.benchmark_ci.models import JobContext, JobResult


class IncBuildPullJob(JobExecutor):
    """Pull inc-build image from registry.

    This job:
    1. Checks if inc_build is enabled in project.yaml
    2. Pulls the inc-build image from registry
    3. Fails if pull fails (image must exist)

    Other jobs can then use use_inc_build=True for faster builds.
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,  # noqa: ARG002
    ) -> JobResult:
        """Execute inc-build image pull.

        Steps:
        1. Check if inc_build is enabled
        2. Pull inc-build image from registry
        """
        try:
            logger.info("=== INC-BUILD PULL ===")
            logger.info(f"Pulling inc-build image for {job.benchmark}")

            # Load project config to check if inc_build is enabled
            project_config = self._load_project_config(job.benchmark)
            if not project_config.inc_build:
                logger.info("inc_build is not enabled for this benchmark, skipping")
                return JobResult(success=True)

            # Pull inc-build image
            infra = self._get_infra()
            pulled = infra.pull_inc_build_image(
                project_name=job.benchmark,
                sanitizer=job.sanitizer,
            )

            if not pulled:
                return JobResult(
                    success=False,
                    error_message=f"Failed to pull inc-build image for {job.benchmark}",
                )

            logger.info(f"Inc-build image pulled successfully for {job.benchmark}")
            return JobResult(success=True)

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))
