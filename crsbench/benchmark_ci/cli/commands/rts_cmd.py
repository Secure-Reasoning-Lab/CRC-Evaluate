"""Regression test selection check subcommand."""

import argparse
import json
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
from crsbench.benchmark_ci.jobs.ci_checks import RtsCheckJob
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import (
    BenchmarkValidator,
    _load_project_capabilities,
)
from crsbench.executor import DAGExecutor
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the rts subcommand."""
    parser = subparsers.add_parser(
        "rts",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run regression test selection checks",
    )
    parser.set_defaults(ci_func=run_rts)


def run_rts(args: argparse.Namespace) -> int:
    """Run regression test selection checks via DAG executor."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    source_mode = getattr(args, "source", "main_repo")
    build_workers = getattr(args, "build_workers", 4)
    verify_workers = getattr(args, "verify_workers", 4)
    use_inc_build = not getattr(args, "no_inc_build", False)
    force_rebuild = getattr(args, "force_rebuild", False)
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)

    validator = BenchmarkValidator(
        build_workers=build_workers,
        verify_workers=verify_workers,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
    )

    build_mode = "inc-build" if use_inc_build else "full-build"
    logger.info(
        f"Running rts: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, {build_mode}"
    )

    # Pre-compute per-benchmark capabilities
    benchmark_configs = []
    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_configs.append((path, supports_inc, rts_mode, effective_inc))

    # Build DAG: one RtsCheckJob per benchmark
    jobs = [
        RtsCheckJob(
            benchmark_path=path,
            validator=validator,
            use_inc_build=effective_inc,
            force_rebuild=force_rebuild,
            rts_mode=rts_mode,
        )
        for path, _, rts_mode, effective_inc in benchmark_configs
    ]

    # Execute via DAG executor
    start_dt = datetime.now()
    executor = DAGExecutor(max_workers=build_workers)
    dag_results = executor.execute(jobs, JobContext())

    # Build summary from DAG results
    summary = ValidationSummary(started_at=start_dt)

    for path, supports_inc, rts_mode, _ in benchmark_configs:
        job_id = f"rts-check:{path.name}"
        patch_rts_result = None

        if job_id in dag_results:
            exec_result = dag_results[job_id]
            if exec_result.job_result and exec_result.job_result.details:
                patch_rts_result = exec_result.job_result.details.get("check_result")
            if patch_rts_result is None:
                error = exec_result.error or "DAG execution failed"
                patch_rts_result = CheckResult.make_error(error)
        else:
            patch_rts_result = CheckResult.make_error("Job not found in DAG results")

        summary.add_result(
            BenchmarkValidationResult(
                benchmark=path.name,
                benchmark_path=path,
                patch_rts_check=patch_rts_result,
                supports_inc_build=supports_inc,
                rts_mode=rts_mode,
                started_at=start_dt,
                finished_at=datetime.now(),
            )
        )

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
