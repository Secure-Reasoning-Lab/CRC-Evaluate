"""Upfront build orchestration with deduplication.

Creates BuildVariantsJob instances for unique (benchmark, sanitizer) tuples
and executes them via DAGExecutor. Build results are stored in JobContext.shared
for downstream CRS trial jobs to consume.
"""

from typing import TYPE_CHECKING, cast

from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob
from crsbench.executor.dag import DAGExecutor
from crsbench.executor.types import ExecutorResult
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


# Type alias for Trial-like objects with required fields
type TrialLike = object


def create_upfront_build_jobs(
    trials: list[TrialLike],
    *,
    use_inc_build: bool = True,
    force_rebuild: bool = False,
    source_mode: str = "main_repo",
) -> list[BuildVariantsJob]:
    """Create deduplicated build jobs from trial list.

    Args:
        trials: List of Trial objects (must have benchmark_harness.path and sanitizer)
        use_inc_build: Whether to use incremental builds (default True)
        force_rebuild: Whether to force rebuilding even if cached (default False)
        source_mode: Source mode - 'main_repo' (git clone) or 'pkgs' (bundled tarballs)

    Returns:
        List of BuildVariantsJob, one per unique (benchmark_path, sanitizer) tuple
    """
    # Deduplicate by (benchmark_path, sanitizer)
    seen: dict[tuple["Path", str], BuildVariantsJob] = {}

    for trial in trials:
        # Access benchmark_harness.path and sanitizer from trial
        # Trial is a Pydantic model with benchmark_harness: BenchmarkHarness and sanitizer: str
        benchmark_harness = getattr(trial, "benchmark_harness", None)
        if benchmark_harness is None:
            logger.warning("Trial missing benchmark_harness, skipping")
            continue

        benchmark_path = getattr(benchmark_harness, "path", None)
        sanitizer = getattr(trial, "sanitizer", "address")

        if benchmark_path is None:
            logger.warning("Trial missing benchmark_harness.path, skipping")
            continue

        key = (benchmark_path, sanitizer)
        if key in seen:
            continue

        benchmark_name = benchmark_path.name
        job = BuildVariantsJob(
            benchmark_path=benchmark_path,
            benchmark_name=benchmark_name,
            use_inc_build=use_inc_build,
            force_rebuild=force_rebuild,
            source_mode=source_mode,
        )
        seen[key] = job

    jobs = list(seen.values())

    logger.info(
        "Created %d build jobs from %d trials (deduplication: %d unique builds)",
        len(jobs),
        len(trials),
        len(jobs),
    )

    return jobs


def execute_upfront_builds(
    jobs: list[BuildVariantsJob],
    *,
    build_workers: int = 2,
) -> tuple[dict[str, ExecutorResult], JobContext]:
    """Execute build jobs via DAGExecutor with typed concurrency.

    Args:
        jobs: List of BuildVariantsJob to execute
        build_workers: Maximum concurrent build jobs (default 2)

    Returns:
        Tuple of (results dict, JobContext with shared data populated)
    """
    context = JobContext()

    if not jobs:
        logger.info("No build jobs to execute")
        return {}, context

    executor = DAGExecutor(type_limits={"build": build_workers})

    logger.info("Executing %d build jobs with %d workers", len(jobs), build_workers)

    results = executor.execute(cast("list[Job]", jobs), context)

    success_count = sum(1 for r in results.values() if r.success)
    failed_count = len(results) - success_count

    logger.info(
        "Build execution complete: %d succeeded, %d failed",
        success_count,
        failed_count,
    )

    return results, context
