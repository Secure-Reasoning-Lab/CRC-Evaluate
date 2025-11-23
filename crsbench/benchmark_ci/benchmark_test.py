#!/usr/bin/env python3
"""Main orchestrator for benchmark end-to-end testing.

This script validates that benchmarks are correctly formatted and functional:
- File format validation (meta.yaml, project.yaml, POV files)
- Build verification with different engines and sanitizers
- POV reproduction testing
- Patch verification
- test.sh execution validation

Usage:
    # Test all benchmarks
    python -m crsbench.benchmark_ci.benchmark_test --run_mode all

    # Test specific benchmark
    python -m crsbench.benchmark_ci.benchmark_test --run_mode project --project curl-delta-02

    # Quick test with default sanitizer/engine only
    python -m crsbench.benchmark_ci.benchmark_test --check_default_only
"""

import argparse
import os
import sys
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Set, Optional, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from crsbench.benchmark_ci.utils import (
    JobContext,
    ExecJobType,
    TaskMode,
    get_benchmarks_root,
    get_oss_fuzz_root,
)
from crsbench.benchmark_ci.file_validator import (
    check_benchmark_files,
    get_tasks,
    get_harnesses,
    get_project_config,
)
from crsbench.benchmark_ci.execution_validator import check_benchmark_execution
from crsbench.utils.logger import get_logger
from crsbench.utils import log_section

logger = get_logger(__name__)


def get_all_benchmarks() -> Set[str]:
    """Get all benchmark names from benchmarks directory.

    Returns:
        Set of benchmark names
    """
    benchmarks_root = get_benchmarks_root()
    benchmarks_dir = Path(benchmarks_root)

    benchmarks = set()

    for item in benchmarks_dir.iterdir():
        if item.is_dir() and (item / ".aixcc").exists():
            benchmarks.add(item.name)

    return benchmarks


def get_benchmarks_to_test(
    run_mode: str = "all",
    project: Optional[str] = None,
    projects: Optional[Set[str]] = None,
) -> Set[str]:
    """Get benchmarks to test based on run mode.

    Args:
        run_mode: "all" or "project"
        project: Project name (required if run_mode is "project")
        projects: Set of project names (overrides run_mode if provided)

    Returns:
        Set of benchmark names to test
    """
    # If projects is specified, use it (overrides run_mode)
    if projects:
        return projects

    if run_mode == "project":
        if not project:
            raise Exception("[Error] Project name must be provided when run_mode is 'project'")
        return {project}
    else:
        return get_all_benchmarks()


def ensure_benchmark_symlink(benchmark: str) -> None:
    """Ensure symlink exists for benchmark in oss-fuzz/projects/.

    Creates symlink on-demand if it doesn't exist.

    Args:
        benchmark: Benchmark name
    """
    benchmarks_root = Path(get_benchmarks_root())
    oss_fuzz_projects = Path(get_oss_fuzz_root()) / "projects"

    target_link = oss_fuzz_projects / benchmark
    source_path = benchmarks_root / benchmark

    # Check if benchmark exists
    if not source_path.exists():
        raise FileNotFoundError(f"Benchmark not found: {source_path}")

    # If symlink already exists and points to correct location, we're done
    if target_link.exists():
        if target_link.is_symlink():
            existing_target = os.readlink(target_link)
            relative_path = os.path.relpath(source_path, oss_fuzz_projects)
            if existing_target == relative_path:
                return  # Already exists and correct
            else:
                logger.warning(f"Symlink exists but points to wrong location: {benchmark}")
                logger.warning(f"  Current: {existing_target}")
                logger.warning(f"  Expected: {relative_path}")
                return
        else:
            logger.warning(f"Path exists but is not a symlink: {target_link}")
            return

    # Create symlink
    relative_path = os.path.relpath(source_path, oss_fuzz_projects)
    try:
        os.symlink(relative_path, target_link)
        logger.info(f"Created symlink: {benchmark} -> {relative_path}")
    except OSError as e:
        raise RuntimeError(f"Failed to create symlink for {benchmark}: {e}")


def is_project_disabled(benchmark: str) -> bool:
    """Check if a project is disabled in project.yaml.

    Args:
        benchmark: Benchmark name

    Returns:
        True if disabled
    """
    try:
        project_config = get_project_config(benchmark)
        return project_config.get("disabled", False)
    except Exception as e:
        logger.warning(f"Could not load project config for {benchmark}: {e}")
        return True


def get_workload(
    benchmarks: Set[str],
    check_default_only: bool,
    job_types: Optional[Set[ExecJobType]] = None,
) -> List[JobContext]:
    """Generate test jobs for benchmarks.

    Args:
        benchmarks: Set of benchmark names
        check_default_only: Only test libfuzzer + address/none sanitizers
        job_types: Optional set of job types to filter (None means all types)

    Returns:
        List of JobContext objects
    """
    jobs: List[JobContext] = []

    for benchmark in sorted(benchmarks):
        if is_project_disabled(benchmark):
            logger.info(f"Skipping disabled benchmark: {benchmark}")
            continue

        logger.info(f"Generating jobs for benchmark: {benchmark}")

        try:
            # Get project configuration
            project_config = get_project_config(benchmark)
            language = project_config.get("language", "c")
            sanitizers = project_config.get("sanitizers", ["address"])
            fuzzing_engines = project_config.get("fuzzing_engines", ["libfuzzer"])

            # Get tasks (delta/full mode)
            tasks = get_tasks(benchmark)

            # Get harnesses
            harnesses = get_harnesses(benchmark)

            # Generate jobs for each combination
            for task in tasks:
                for engine in fuzzing_engines:
                    for sanitizer in sanitizers:
                        # Quick check mode: only test default sanitizer/engine
                        if check_default_only:
                            if sanitizer not in ["none", "address"]:
                                continue
                            if engine != "libfuzzer":
                                continue

                        # Delta mode: test base and ref commits
                        if task.mode == TaskMode.DELTA:
                            if not job_types or ExecJobType.DELTA_BASE_CHECK in job_types:
                                jobs.append(JobContext(
                                    job_type=ExecJobType.DELTA_BASE_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                ))

                            if not job_types or ExecJobType.DELTA_REF_CHECK in job_types:
                                jobs.append(JobContext(
                                    job_type=ExecJobType.DELTA_REF_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                ))

                        # Full mode: test base commit
                        if task.mode == TaskMode.FULL:
                            if not job_types or ExecJobType.FULL_BASE_CHECK in job_types:
                                jobs.append(JobContext(
                                    job_type=ExecJobType.FULL_BASE_CHECK,
                                    task=task,
                                    benchmark=benchmark,
                                    language=language,
                                    engine=engine,
                                    sanitizer=sanitizer,
                                ))

                        # Patch checks for each vulnerability
                        if not job_types or ExecJobType.PATCH_CHECK in job_types:
                            for harness in harnesses:
                                for vuln in harness.vulnerabilities:
                                    for pov in vuln.povs:
                                        # Only test if sanitizer matches and patch exists
                                        if (pov.sanitizer == sanitizer and
                                            engine == "libfuzzer" and
                                            vuln.patch_path):

                                            jobs.append(JobContext(
                                                job_type=ExecJobType.PATCH_CHECK,
                                                task=task,
                                                benchmark=benchmark,
                                                language=language,
                                                engine=engine,
                                                sanitizer=sanitizer,
                                                harness=harness,
                                                vulnerability=vuln,
                                                pov=pov,
                                            ))

                # Add test.sh check (once per benchmark)
                if not job_types or ExecJobType.TEST_SH_CHECK in job_types:
                    jobs.append(JobContext(
                        job_type=ExecJobType.TEST_SH_CHECK,
                        task=tasks[0] if tasks else None,
                        benchmark=benchmark,
                        language=language,
                        engine="libfuzzer",
                        sanitizer="address",
                    ))

        except Exception as e:
            logger.error(f"Failed to generate jobs for {benchmark}: {e}")
            traceback.print_exc()

    # Sort jobs for consistent ordering
    return sorted(jobs)


def run_file_checks(
    skip_file_check: bool,
    benchmarks: Set[str]
) -> None:
    """Run file format validation for benchmarks.

    Args:
        skip_file_check: Whether to skip file checks
        benchmarks: Set of benchmark names to check
    """
    if skip_file_check:
        logger.info("Skipping file checks")
        return

    log_section(f"Running file checks for {len(benchmarks)} benchmarks", width=80)

    for benchmark in sorted(benchmarks):
        if is_project_disabled(benchmark):
            logger.info(f"Skipping disabled benchmark: {benchmark}")
            continue

        # Ensure symlink exists before checking files
        try:
            ensure_benchmark_symlink(benchmark)
        except Exception as e:
            logger.error(f"Failed to setup symlink for {benchmark}: {e}")
            traceback.print_exc()
            raise

        try:
            check_benchmark_files(benchmark)
        except Exception as e:
            logger.error(f"File check failed for {benchmark}: {e}")
            traceback.print_exc()
            raise


def _execute_benchmark_jobs(
    benchmark: str,
    jobs: List[JobContext],
    exit_on_error: bool,
    output_dir: Optional[str] = None,
    enable_check_build: bool = False,
) -> Tuple[str, List[Tuple[JobContext, Exception]]]:
    """Execute all jobs for a single benchmark.

    This function runs in a separate process when using parallel execution.

    Args:
        benchmark: Benchmark name
        jobs: List of jobs to execute for this benchmark
        exit_on_error: Whether to exit immediately on error
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation

    Returns:
        Tuple of (benchmark_name, list_of_failed_jobs)
    """
    failed_jobs = []

    logger.info("-" * 80)
    logger.info(f"Processing: {benchmark} ({len(jobs)} jobs)")
    logger.info("-" * 80)

    # Ensure symlink exists before executing jobs
    try:
        ensure_benchmark_symlink(benchmark)
    except Exception as e:
        logger.error(f"[{benchmark}] Failed to setup symlink")
        logger.error(str(e))
        failed_jobs.append((jobs[0] if jobs else None, e))
        return benchmark, failed_jobs

    for i, job in enumerate(jobs, 1):
        logger.info(f"Starting job: {benchmark} | {job.job_type.value} | {job.engine}/{job.sanitizer}")
        logger.info(f"[{benchmark}] Job {i}/{len(jobs)}")

        try:
            check_benchmark_execution(job, output_dir=output_dir, enable_check_build=enable_check_build)
            logger.success(f"Job completed successfully")
        except Exception as e:
            logger.error(f"Job failed: {job.job_type.value}")
            logger.error(f"Reason: {str(e)[:200]}")  # Limit error message length

            failed_jobs.append((job, e))

            if exit_on_error:
                logger.warning(f"Exiting due to --exit_on_error flag")
                break

    # Print benchmark summary
    passed = len(jobs) - len(failed_jobs)
    logger.info(f"Benchmark {benchmark} summary: {passed}/{len(jobs)} passed, {len(failed_jobs)} failed")

    return benchmark, failed_jobs


def run_execution_checks(
    skip_execution_check: bool,
    jobs: List[JobContext],
    exit_on_error: bool,
    workers: int = 1,
    output_dir: Optional[str] = None,
    enable_check_build: bool = False,
) -> None:
    """Run execution validation for jobs.

    Args:
        skip_execution_check: Whether to skip execution checks
        jobs: List of jobs to execute
        exit_on_error: Whether to exit immediately on error
        workers: Number of parallel workers (1 = sequential, >1 = parallel by benchmark)
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation after building
    """
    if skip_execution_check:
        logger.warning("Skipping execution checks")
        return

    logger.info("=" * 80)
    logger.info(f"EXECUTION CHECKS ({len(jobs)} jobs)")
    logger.info("=" * 80)
    if workers > 1:
        logger.info(f"Using {workers} parallel workers (benchmark-level parallelism)")
    else:
        logger.info(f"Using sequential execution")

    # Group jobs by benchmark
    jobs_by_benchmark: Dict[str, List[JobContext]] = defaultdict(list)
    for job in jobs:
        jobs_by_benchmark[job.benchmark].append(job)

    logger.info(f"Grouped {len(jobs)} jobs into {len(jobs_by_benchmark)} benchmarks")
    for benchmark, benchmark_jobs in jobs_by_benchmark.items():
        logger.info(f"  - {benchmark}: {len(benchmark_jobs)} jobs")

    all_failed_jobs = []

    if workers == 1:
        # Sequential execution (original behavior)
        for benchmark in sorted(jobs_by_benchmark.keys()):
            benchmark_jobs = jobs_by_benchmark[benchmark]
            _, failed_jobs = _execute_benchmark_jobs(benchmark, benchmark_jobs, exit_on_error, output_dir, enable_check_build)
            all_failed_jobs.extend(failed_jobs)

            if exit_on_error and failed_jobs:
                break
    else:
        # Parallel execution by benchmark
        logger.info(f"Starting parallel execution with {workers} workers...")

        with ProcessPoolExecutor(max_workers=workers) as executor:
            # Submit all benchmarks to the pool
            future_to_benchmark = {
                executor.submit(_execute_benchmark_jobs, benchmark, benchmark_jobs, exit_on_error, output_dir, enable_check_build): benchmark
                for benchmark, benchmark_jobs in jobs_by_benchmark.items()
            }

            # Process results as they complete
            for future in as_completed(future_to_benchmark):
                benchmark = future_to_benchmark[future]
                try:
                    benchmark_name, failed_jobs = future.result()
                    all_failed_jobs.extend(failed_jobs)

                    if exit_on_error and failed_jobs:
                        logger.warning(f"Cancelling remaining benchmarks due to --exit_on_error flag")
                        # Cancel pending futures
                        for pending_future in future_to_benchmark:
                            if not pending_future.done():
                                pending_future.cancel()
                        break

                except Exception as e:
                    logger.error(f"Benchmark {benchmark} raised exception: {e}")
                    traceback.print_exc()

    # Print final summary
    total_jobs = len(jobs)
    passed_jobs = total_jobs - len(all_failed_jobs)
    logger.info("=" * 80)
    logger.info(f"FINAL SUMMARY: {passed_jobs}/{total_jobs} jobs passed, {len(all_failed_jobs)} failed")
    logger.info("=" * 80)

    if all_failed_jobs:
        logger.error("=" * 80)
        logger.error("FAILED JOBS")
        logger.error("=" * 80)
        for idx, (job, error_msg) in enumerate(all_failed_jobs, 1):
            logger.error(f"{idx}. {job.benchmark} - {job.job_type.value} ({job.engine}/{job.sanitizer})")
            logger.error(f"   Error: {str(error_msg)[:200]}")
        raise Exception(f"{len(all_failed_jobs)} jobs failed")


def main(
    run_mode: str = "all",
    check_default_only: bool = False,
    skip_file_check: bool = False,
    skip_execution_check: bool = False,
    exit_on_error: bool = False,
    project: Optional[str] = None,
    projects: Optional[Set[str]] = None,
    job_types: Optional[Set[ExecJobType]] = None,
    workers: int = 1,
    output_dir: Optional[str] = None,
    enable_check_build: bool = False,
) -> int:
    """Main entry point for benchmark testing.

    Args:
        run_mode: "all" or "project"
        check_default_only: Only test libfuzzer + address/none
        skip_file_check: Skip file validation
        skip_execution_check: Skip execution validation
        exit_on_error: Exit immediately on error
        project: Project name (required if run_mode is "project")
        projects: Set of project names (overrides run_mode if provided)
        job_types: Optional set of job types to filter (None means all types)
        workers: Number of parallel workers (1 = sequential, >1 = parallel by benchmark)
        output_dir: Directory to save test artifacts (logs, crash outputs, etc.)
        enable_check_build: Enable check_build validation (slower, default: False)

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    ret_code = 0

    try:
        logger.info("=" * 80)
        logger.info("CRSBench Benchmark End-to-End Testing")
        logger.info("=" * 80)
        if projects:
            logger.info(f"Testing specific projects: {', '.join(sorted(projects))}")
        else:
            logger.info(f"Run mode: {run_mode}")

        # Get benchmarks to test
        benchmarks = get_benchmarks_to_test(run_mode, project, projects)
        logger.info(f"Found {len(benchmarks)} benchmarks to test")
        if projects:
            logger.info(f"Benchmarks: {', '.join(sorted(benchmarks))}")

        # Run file checks
        run_file_checks(skip_file_check, benchmarks)

        # Generate jobs
        jobs = get_workload(benchmarks, check_default_only, job_types)
        logger.info(f"Generated {len(jobs)} test jobs")
        if job_types:
            logger.info(f"Filtered to job types: {', '.join(jt.value for jt in job_types)}")
        if not enable_check_build:
            logger.info("check_build is disabled (use --enable-check-build to enable)")

        # Setup output directory if specified
        if output_dir:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Saving artifacts to: {output_path.absolute()}")

        # Run execution checks
        run_execution_checks(skip_execution_check, jobs, exit_on_error, workers, output_dir, enable_check_build)

        logger.info("=" * 80)
        logger.info("ALL TESTS PASSED ✓")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"Benchmark testing failed")
        logger.error(str(e))
        ret_code = 1

    return ret_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CRSBench Benchmark End-to-End Testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--run_mode",
        choices=["all", "project"],
        default="all",
        help="Run mode: all (default) or project"
    )

    parser.add_argument(
        "--check_default_only",
        action="store_true",
        help="Only test libfuzzer + address/none sanitizers (default: False)"
    )

    parser.add_argument(
        "--skip_file_check",
        action="store_true",
        help="Skip file format validation (default: False)"
    )

    parser.add_argument(
        "--skip_execution_check",
        action="store_true",
        help="Skip execution validation (default: False)"
    )

    parser.add_argument(
        "--exit_on_error",
        action="store_true",
        help="Exit immediately on first error (default: False)"
    )

    parser.add_argument(
        "--project",
        type=str,
        help="Project name (required if run_mode is 'project')"
    )

    parser.add_argument(
        "--projects",
        type=str,
        help="Comma-separated list of project names to test (overrides --run_mode). "
             "Example: --projects afc-curl-delta-01,afc-curl-delta-02,afc-tika-delta-01"
    )

    parser.add_argument(
        "--job_types",
        type=str,
        help="Comma-separated list of job types to run (default: all). "
             "Available types: delta_base_check, delta_ref_check, full_base_check, "
             "patch_check, test_sh_check. Example: --job_types delta_base_check,test_sh_check"
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers for benchmark execution (default: 1 = sequential). "
             "Benchmarks are processed in parallel, with each benchmark's jobs running sequentially. "
             "Example: --workers 4 to run up to 4 benchmarks in parallel"
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        help="Directory to save test artifacts (logs, crash outputs, etc.). "
             "Example: --output-dir ./ci-results"
    )

    parser.add_argument(
        "--enable-check-build",
        action="store_true",
        help="Enable check_build validation after building (slower, default: False). "
             "By default, check_build is disabled to speed up testing."
    )

    args = parser.parse_args()

    # Parse projects
    projects_filter = None
    if args.projects:
        try:
            projects_filter = set()
            for project_name in args.projects.split(','):
                project_name = project_name.strip()
                if project_name:
                    projects_filter.add(project_name)

            if not projects_filter:
                logger.error("--projects specified but no valid project names provided")
                sys.exit(1)

            logger.info(f"Parsed {len(projects_filter)} projects from --projects flag")
        except Exception as e:
            logger.error(f"Failed to parse --projects: {e}")
            sys.exit(1)

    # Parse job_types
    job_types_filter = None
    if args.job_types:
        try:
            job_types_filter = set()
            for job_type_str in args.job_types.split(','):
                job_type_str = job_type_str.strip()
                # Map string to ExecJobType enum
                try:
                    job_type = ExecJobType(job_type_str)
                    job_types_filter.add(job_type)
                except ValueError:
                    logger.error(f"Invalid job type: {job_type_str}")
                    logger.error(f"Valid types: {', '.join(jt.value for jt in ExecJobType)}")
                    sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to parse --job_types: {e}")
            sys.exit(1)

    sys.exit(main(
        run_mode=args.run_mode,
        check_default_only=args.check_default_only,
        skip_file_check=args.skip_file_check,
        skip_execution_check=args.skip_execution_check,
        exit_on_error=args.exit_on_error,
        project=args.project,
        projects=projects_filter,
        job_types=job_types_filter,
        workers=args.workers,
        output_dir=args.output_dir,
        enable_check_build=args.enable_check_build,
    ))
