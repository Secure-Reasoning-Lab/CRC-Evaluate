"""Post-trial analysis orchestration using flat jobs + Redis.

After CRS trials complete, this module generates coverage collection and patch
verification jobs from trial results. Reuses existing flat jobs for consistent
architecture with CI commands.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from crsbench.benchmark_ci.jobs.base import Job
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
)
from crsbench.executor.types import ExecutorResult
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


@dataclass
class TrialResult:
    """Result from a CRS trial execution.

    Captures the essential outcome from a CRS trial for post-trial analysis:
    - For bug-finding CRS: cpvs_found indicates discovered vulnerabilities
    - For bug-fixing CRS: patches contains generated patch files
    """

    trial_id: str
    benchmark_path: Path
    harness_name: str
    trial_output_dir: Path
    success: bool
    crs_type: str  # "bug_finding" or "bug_fixing"
    cpvs_found: list[str] = field(default_factory=list)
    patches: list[Path] = field(default_factory=list)


def create_post_trial_jobs(
    trial_results: list[TrialResult],
    build_job_ids: dict[str, str],
    *,
    coverage_enabled: bool = True,
    source_mode: str = "pkgs",
) -> list[Job]:
    """Create post-trial analysis jobs from trial results.

    Args:
        trial_results: List of TrialResult from completed CRS trials
        build_job_ids: Maps benchmark_name to build job ID for dependency
        coverage_enabled: Whether to create coverage collection jobs
        source_mode: Source mode - 'main_repo' (git clone) or 'pkgs' (bundled tarballs)

    Returns:
        List of jobs with proper dependency ordering:
        - FlatCollectCoverageJob for successful trials (if coverage_enabled)
        - BuildPatchVariantJob + PatchVariantTestJob pairs for bug-fixing trials

    Note:
        Failed trials are skipped. No jobs are created for trials where
        success=False.
    """
    if not trial_results:
        logger.info("No trial results provided, returning empty job list")
        return []

    jobs: list[Job] = []
    coverage_count = 0
    patch_build_count = 0
    patch_test_count = 0

    for result in trial_results:
        # Skip failed trials
        if not result.success:
            logger.debug(
                "Skipping failed trial %s for post-trial analysis",
                result.trial_id,
            )
            continue

        benchmark_name = result.benchmark_path.name
        build_job_id = build_job_ids.get(benchmark_name, "")

        # Create coverage job if enabled
        if coverage_enabled:
            coverage_job = FlatCollectCoverageJob(
                benchmark_path=result.benchmark_path,
                benchmark_name=benchmark_name,
                harness=result.harness_name,
                build_job_id=build_job_id,
                source_mode=source_mode,
            )
            jobs.append(coverage_job)
            coverage_count += 1

        # Create patch jobs for bug-fixing CRS with patches
        if result.crs_type == "bug_fixing" and result.patches:
            # Load adapter once per benchmark to resolve per-CPV sanitizers
            adapter = _load_adapter(result.benchmark_path, source_mode)

            for patch_path in result.patches:
                patch_id = patch_path.stem
                cpv_id = _extract_cpv_id(result, patch_path)

                # Resolve sanitizer for this CPV
                cpv_sanitizer = "address"
                if adapter:
                    cpv_sanitizer = adapter.get_cpv_sanitizer(
                        result.harness_name, cpv_id
                    )

                # Create build job for patched variant
                build_patch_job = BuildPatchVariantJob(
                    benchmark_path=result.benchmark_path,
                    benchmark_name=benchmark_name,
                    cpv_id=cpv_id,
                    patch_id=patch_id,
                    patch_path=patch_path,
                    harness=result.harness_name,
                    sanitizer=cpv_sanitizer,
                    use_inc_build=True,
                    force_rebuild=False,
                    build_job_id=build_job_id,
                    source_mode=source_mode,
                )
                jobs.append(build_patch_job)
                patch_build_count += 1

                # Create test job for patched variant
                # Get POV paths from trial output dir
                pov_paths = _get_pov_paths(result.trial_output_dir, cpv_id)

                test_patch_job = PatchVariantTestJob(
                    benchmark_path=result.benchmark_path,
                    benchmark_name=benchmark_name,
                    cpv_id=cpv_id,
                    patch_id=patch_id,
                    harness=result.harness_name,
                    pov_paths=pov_paths,
                    test_mode="FULL",
                    build_patch_job_id=build_patch_job.job_id,
                )
                jobs.append(test_patch_job)
                patch_test_count += 1

    logger.info(
        "Created %d post-trial jobs: %d coverage, %d patch builds, %d patch tests",
        len(jobs),
        coverage_count,
        patch_build_count,
        patch_test_count,
    )

    return jobs


def execute_post_trial_analysis(
    jobs: list[Job],
    redis_host: str = "localhost",
    *,
    queue_name: str = "crsbench_post_trial_verify",
) -> dict[str, ExecutorResult]:
    """Execute post-trial analysis jobs via Redis.

    Args:
        jobs: List of post-trial jobs from create_post_trial_jobs
        redis_host: Redis server hostname
        queue_name: Redis queue name for post-trial jobs

    Returns:
        Dict mapping job_id to ExecutorResult for every job
    """
    if not jobs:
        logger.info("No post-trial jobs to execute")
        return {}

    from crsbench.distributed.ci_jobs import (
        ci_results_to_executor_results,
        enqueue_and_poll_ci_jobs,
    )

    logger.info("Executing %d post-trial jobs via Redis (%s)", len(jobs), redis_host)

    raw_results = enqueue_and_poll_ci_jobs(jobs, redis_host, queue_name=queue_name)
    results = ci_results_to_executor_results(raw_results)

    success_count = sum(1 for r in results.values() if r.success)
    failed_count = len(results) - success_count

    logger.info(
        "Post-trial analysis complete: %d succeeded, %d failed",
        success_count,
        failed_count,
    )

    return results


def _load_adapter(
    benchmark_path: Path, source_mode: str
) -> Optional["MetaYamlAdapter"]:
    """Load benchmark adapter for sanitizer lookup.

    Returns None (with warning) if adapter cannot be loaded.
    """
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import get_oss_fuzz_root

    oss_fuzz_path = Path(get_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    adapter = engine.load_adapter(benchmark_path)
    if not adapter:
        logger.warning(
            "Failed to load adapter for %s, using default sanitizer",
            benchmark_path,
        )
    return adapter


def _extract_cpv_id(result: TrialResult, patch_path: Path) -> str:
    """Extract CPV ID from trial result or patch path.

    Args:
        result: TrialResult with cpvs_found
        patch_path: Path to patch file

    Returns:
        CPV ID string (e.g., "cpv_0")
    """
    # Try to extract from patch filename (e.g., "cpv_0_patch.diff" -> "cpv_0")
    patch_name = patch_path.stem
    for cpv in result.cpvs_found:
        if cpv in patch_name:
            return cpv

    # Fall back to first found CPV or derive from patch name
    if result.cpvs_found:
        return result.cpvs_found[0]

    # Last resort: extract cpv_N pattern from filename
    parts = patch_name.split("_")
    if len(parts) >= 2 and parts[0] == "cpv":
        return f"cpv_{parts[1]}"

    return "cpv_0"


def _get_pov_paths(trial_output_dir: Path, cpv_id: str) -> list[Path]:
    """Get POV file paths from trial output directory.

    Args:
        trial_output_dir: Trial output directory
        cpv_id: CPV ID to filter POVs

    Returns:
        List of POV file paths
    """
    pov_dir = trial_output_dir / "output" / "povs"
    if not pov_dir.exists():
        return []

    pov_paths = []
    for pov_file in pov_dir.glob("*"):
        if pov_file.is_file():
            # Include all POVs or filter by CPV ID
            if cpv_id in pov_file.stem or cpv_id == "cpv_0":
                pov_paths.append(pov_file)

    return pov_paths
