"""Run all validation checks subcommand.

Uses DAG executor to fan out POV, patch+RTS, and coverage checks in parallel
after format validation passes. Single build mode per invocation
(inc-build default, --no-inc-build for full).
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.jobs.base import JobContext
from crsbench.benchmark_ci.jobs.ci_checks import (
    CoverageCheckJob,
    PatchRtsCheckJob,
    PovCheckJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import (
    BenchmarkValidator,
    _load_project_capabilities,
)
from crsbench.executor import DAGExecutor
from crsbench.utils.logger import get_logger
from crsbench.validation import validate_benchmark as format_validate

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the all subcommand."""
    parser = subparsers.add_parser(
        "all",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run all validation checks",
    )
    parser.set_defaults(ci_func=run_all)


def _run_format_check(path: Path) -> CheckResult:
    """Run format validation on a benchmark path."""
    start_time = time.time()
    try:
        result = format_validate(path)
        elapsed = time.time() - start_time
        if result.is_valid:
            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                details={"warnings": result.warning_count},
            )
        error_msgs = "; ".join(e.message for e in result.errors[:3])
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error=error_msgs,
            details={
                "error_count": result.error_count,
                "warning_count": result.warning_count,
            },
        )
    except Exception as exc:
        elapsed = time.time() - start_time
        return CheckResult.make_error(str(exc), time_seconds=elapsed)


def _validate_benchmark(
    path: Path,
    validator: BenchmarkValidator,
    *,
    use_inc_build: bool,
) -> BenchmarkValidationResult:
    """Run all checks for a single benchmark using DAG executor.

    Strategy:
    1. Format check runs first (fast gate)
    2. If format passes, POV/Patch+RTS/Coverage fan out in parallel via DAG
    3. Single build mode: inc-build default, --no-inc-build for full
    4. RTS runs on same build as patch tests (shared work_dir in PatchRtsCheckJob)
    5. CI all always force-rebuilds (no cache)
    """
    start_dt = datetime.now()
    supports_inc, rts_mode = _load_project_capabilities(path)

    # Format check: fast gate before heavy checks
    format_check = _run_format_check(path)

    # If format fails, skip everything else
    if format_check.status == CheckStatus.FAIL:
        return BenchmarkValidationResult(
            benchmark=path.name,
            benchmark_path=path,
            format_check=format_check,
            supports_inc_build=supports_inc,
            rts_mode=rts_mode,
            started_at=start_dt,
            finished_at=datetime.now(),
        )

    # Single build mode: inc-build by default (unless --no-inc-build)
    effective_inc = use_inc_build and supports_inc

    # Build DAG: POV, Patch+RTS, Coverage fan out in parallel
    jobs = [
        PovCheckJob(
            benchmark_path=path,
            validator=validator,
            use_inc_build=effective_inc,
            force_rebuild=True,
        ),
        PatchRtsCheckJob(
            benchmark_path=path,
            validator=validator,
            use_inc_build=effective_inc,
            force_rebuild=True,
            rts_mode=rts_mode,
        ),
        CoverageCheckJob(
            benchmark_path=path,
            validator=validator,
            use_inc_build=effective_inc,
            force_rebuild=True,
        ),
    ]

    # Execute DAG: all three jobs have no dependencies, run in parallel
    executor = DAGExecutor(max_workers=3)
    context = JobContext()
    results = executor.execute(jobs, context)

    # Extract results from DAG execution
    pov_result = None
    patch_result = None
    patch_rts_result = None
    coverage_result = None

    pov_job_id = f"pov-check:{path.name}"
    patch_job_id = f"patch-rts-check:{path.name}"
    coverage_job_id = f"coverage-check:{path.name}"

    if pov_job_id in results:
        job_result = results[pov_job_id]
        pov_result = (
            job_result.job_result.details.get("check_result")
            if (job_result.job_result and job_result.job_result.details)
            else None
        )
        if pov_result is None:
            error = job_result.error or "DAG execution failed"
            pov_result = CheckResult.make_error(error)

    if patch_job_id in results:
        job_result = results[patch_job_id]
        if job_result.job_result and job_result.job_result.details:
            patch_result = job_result.job_result.details.get("patch_result")
            patch_rts_result = job_result.job_result.details.get("patch_rts_result")
        if patch_result is None:
            error = job_result.error or "DAG execution failed"
            patch_result = CheckResult.make_error(error)
        if patch_rts_result is None:
            patch_rts_result = CheckResult.skip("No RTS mode configured")

    if coverage_job_id in results:
        job_result = results[coverage_job_id]
        coverage_result = (
            job_result.job_result.details.get("check_result")
            if (job_result.job_result and job_result.job_result.details)
            else None
        )
        if coverage_result is None:
            error = job_result.error or "DAG execution failed"
            coverage_result = CheckResult.make_error(error)

    return BenchmarkValidationResult(
        benchmark=path.name,
        benchmark_path=path,
        format_check=format_check,
        pov_check=pov_result,
        patch_check=patch_result,
        patch_rts_check=patch_rts_result,
        coverage_check=coverage_result,
        supports_inc_build=supports_inc,
        rts_mode=rts_mode,
        started_at=start_dt,
        finished_at=datetime.now(),
    )


def run_all(args: argparse.Namespace) -> int:
    """Run all validation checks on resolved benchmarks.

    Uses DAG executor for parallel fan-out of POV/patch/RTS/coverage
    checks after format validation passes.
    """
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    source_mode = getattr(args, "source", "main_repo")
    build_workers = getattr(args, "build_workers", 4)
    verify_workers = getattr(args, "verify_workers", 4)
    use_inc_build = not getattr(args, "no_inc_build", False)
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)

    validator = BenchmarkValidator(
        build_workers=build_workers,
        verify_workers=verify_workers,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
    )

    build_mode = "inc-build" if use_inc_build else "full-build"
    extras = f", max-povs-per-cpv={max_povs_per_cpv}" if max_povs_per_cpv else ""
    logger.info(
        f"Running all: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}{extras}"
    )

    summary = ValidationSummary(started_at=datetime.now())

    if len(paths) <= 1:
        for path in paths:
            result = _validate_benchmark(path, validator, use_inc_build=use_inc_build)
            summary.add_result(result)
    else:
        with ThreadPoolExecutor(max_workers=build_workers) as pool:
            futures = {
                pool.submit(
                    _validate_benchmark, path, validator, use_inc_build=use_inc_build
                ): path
                for path in paths
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"Unexpected error validating {path.name}: {exc}")
                    result = BenchmarkValidationResult(
                        benchmark=path.name,
                        benchmark_path=path,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                summary.add_result(result)

    summary.finished_at = datetime.now()

    print_results_table(
        summary,
        check_mode=CheckMode.ALL,
        no_color=getattr(args, "no_color", False),
    )

    output_dir = getattr(args, "output_dir", None)
    if output_dir:
        save_output_dir(summary, Path(output_dir), check_mode=CheckMode.ALL)

    output_path = getattr(args, "output", None)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary.to_dict(), indent=2))

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
