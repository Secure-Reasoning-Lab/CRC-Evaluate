"""Coverage check job executor.

Uses CoverageEngine for coverage measurement.
"""

from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.jobs.base import JobExecutor, logger
from crsbench.benchmark_ci.models import JobContext, JobResult, TaskMode
from crsbench.builder.types import VariantType


class CoverageCheckJob(JobExecutor):
    """Check coverage collection works for a benchmark.

    Builds coverage variant and collects coverage from existing corpus/seeds.
    Uses the ref_commit for delta mode, base_commit for full mode.
    """

    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,  # noqa: ARG002
    ) -> JobResult:
        """Execute coverage check using CoverageEngine.

        Steps:
        1. Build coverage variant at appropriate commit
        2. Find corpus/seeds directory
        3. Collect coverage using CoverageEngine
        """
        if not job.task:
            return JobResult(
                success=False, error_message="COVERAGE_CHECK requires task"
            )

        try:
            logger.info("=== COVERAGE CHECK ===")

            # Determine which commit and variant type to use based on mode
            if job.task.mode == TaskMode.DELTA:
                target_commit = job.task.ref_commit
                if not target_commit:
                    return JobResult(
                        success=False,
                        error_message="No ref_commit found for delta mode",
                    )
                variant_type = VariantType.DELTA_REF
                logger.info(f"Using ref_commit: {target_commit[:8]}")
            else:
                target_commit = job.task.base_commit
                variant_type = VariantType.FULL_BASE
                logger.info(f"Using base_commit: {target_commit[:8]}")

            # Step 1: Build coverage variant
            logger.info("Step 1/2: Building coverage variant")
            build_result = self._build_variant(
                benchmark=job.benchmark,
                variant_type=variant_type,
                commit=target_commit,
                sanitizer="coverage",
            )

            if not build_result.success:
                error_msg = (
                    str(build_result.error) if build_result.error else "Build failed"
                )
                return JobResult(success=False, error_message=error_msg)

            # Step 2: Collect coverage
            logger.info("Step 2/2: Collecting coverage")

            benchmark_path = self._get_benchmark_path(job.benchmark)

            # Find corpus directory (check multiple locations)
            corpus_dir = self._find_corpus_dir(benchmark_path, job.harness_name)
            if not corpus_dir:
                logger.warning("No corpus directory found, skipping coverage check")
                return JobResult(success=True)  # Skip without error

            engine = self._get_coverage_engine()

            try:
                harness_filter = job.harness_name
                report = engine.collect_coverage(
                    benchmark_path=benchmark_path,
                    corpus_dir=corpus_dir,
                    harness_filter=harness_filter,
                    force_rebuild=False,  # Already built above
                )

                if report.final_summary:
                    summary = report.final_summary
                    logger.info("Coverage collected successfully:")
                    logger.info(
                        f"  Lines: {summary.lines_covered}/{summary.lines_total} "
                        f"({summary.lines_percent:.1f}%)"
                    )
                    logger.info(
                        f"  Functions: {summary.functions_covered}/{summary.functions_total}"
                    )
                    logger.info(f"  Corpus processed: {summary.corpus_total}")
                    return JobResult(success=True)

                return JobResult(
                    success=False,
                    error_message="Coverage collection failed - no summary",
                )

            finally:
                engine.cleanup()

        except Exception as e:
            return JobResult(success=False, error=e, error_message=str(e))

    def _find_corpus_dir(
        self,
        benchmark_path: Path,
        harness_name: Optional[str] = None,
    ) -> Optional[Path]:
        """Find corpus/seeds directory for coverage collection.

        Checks multiple locations in order of priority:
        1. .aixcc/<harness>/seeds/ (harness-specific seeds)
        2. .aixcc/<harness>/corpus/ (harness-specific corpus)
        3. seeds/ (project-level seeds)
        4. corpus/ (project-level corpus)

        Args:
            benchmark_path: Path to benchmark directory
            harness_name: Optional harness name string

        Returns:
            Path to corpus directory if found, None otherwise
        """
        aixcc_dir = benchmark_path / ".aixcc"

        # Try harness-specific locations first
        if harness_name:
            harness_dir = aixcc_dir / harness_name
            for subdir in ["seeds", "corpus"]:
                corpus_path = harness_dir / subdir
                if corpus_path.exists() and any(corpus_path.iterdir()):
                    logger.info(f"Found corpus at: {corpus_path}")
                    return corpus_path

        # Try project-level locations
        for location in ["seeds", "corpus"]:
            corpus_path = benchmark_path / location
            if corpus_path.exists() and any(corpus_path.iterdir()):
                logger.info(f"Found corpus at: {corpus_path}")
                return corpus_path

        logger.debug(f"No corpus directory found in {benchmark_path}")
        return None
