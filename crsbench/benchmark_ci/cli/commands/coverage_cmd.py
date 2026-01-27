"""Coverage validation subcommand.

Constructs a flat DAG of BuildVariantsJob + FlatCollectCoverageJob,
executed with typed concurrency limits.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.cli.benchmark_discovery import discover_harness_names
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_coverage_result
from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob, FlatCollectCoverageJob
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.executor import DAGExecutor
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the coverage subcommand."""
    parser = subparsers.add_parser(
        "coverage",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run coverage validation",
    )
    parser.set_defaults(ci_func=run_coverage)


def run_coverage(args: argparse.Namespace) -> int:
    """Run coverage validation on resolved benchmarks via flat DAG."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    source_mode = getattr(args, "source", "main_repo")
    build_workers = getattr(args, "build_workers", 4)
    verify_workers = getattr(args, "verify_workers", 4)
    use_inc_build = not getattr(args, "no_inc_build", False)
    force_rebuild = getattr(args, "force_rebuild", True)

    build_mode = "inc-build" if use_inc_build else "full-build"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    logger.info(
        f"Running coverage: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}, {rebuild_mode}"
    )

    # Build flat DAG: BuildVariantsJob -> FlatCollectCoverageJob per benchmark
    all_jobs: list[Job] = []

    for path in paths:
        supports_inc, _ = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_name = path.name

        build_job = BuildVariantsJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            use_inc_build=effective_inc,
            force_rebuild=force_rebuild,
            source_mode=source_mode,
        )
        all_jobs.append(build_job)

        harnesses = discover_harness_names(path)
        harness = harnesses[0] if harnesses else ""

        coverage_job = FlatCollectCoverageJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            harness=harness,
            build_job_id=build_job.job_id,
            source_mode=source_mode,
        )
        all_jobs.append(coverage_job)

    # Log DAG summary
    build_count = sum(1 for j in all_jobs if isinstance(j, BuildVariantsJob))
    coverage_count = sum(1 for j in all_jobs if isinstance(j, FlatCollectCoverageJob))
    logger.info(
        f"DAG: {len(all_jobs)} jobs — {build_count} build, {coverage_count} coverage"
    )

    # Execute with typed concurrency
    start_dt = datetime.now()
    executor = DAGExecutor(
        type_limits={"build": build_workers, "verify": verify_workers}
    )
    output_dir = getattr(args, "output_dir", None)
    context = JobContext(output_dir=Path(output_dir) if output_dir else None)
    dag_results = executor.execute(all_jobs, context)

    # Build summary from DAG results
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)

    for path in paths:
        coverage_result = aggregate_coverage_result(dag_results, path.name)
        build_result = dag_results.get(f"build-variants:{path.name}")
        shared_build = build_result.elapsed_seconds if build_result else 0.0
        storage_bytes = 0
        if build_result and build_result.job_result:
            storage_bytes = build_result.job_result.details.get("storage_bytes", 0)
        summary.add_result(
            BenchmarkValidationResult(
                benchmark=path.name,
                benchmark_path=path,
                coverage_check=coverage_result,
                shared_build_time=shared_build,
                storage_bytes=storage_bytes,
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
