"""Coverage validation subcommand.

Uses _build_dag() from all_cmd to create jobs, then executes via Redis:
Phase 1 — Build jobs (including coverage variant) via Redis build queue
Phase 2 — FlatCollectCoverageJob via Redis verify queue
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
    reject_local_worker_flags_in_distributed,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_coverage_result
from crsbench.benchmark_ci.jobs.flat import (
    BuildSingleVariantJob,
    FlatCollectCoverageJob,
    PrepareIncImageJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

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
    """Run coverage validation on resolved benchmarks via Redis."""
    if not reject_local_worker_flags_in_distributed(args):
        return 1

    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    source_mode = getattr(args, "source", "pkgs")
    mode = getattr(args, "mode", "snapshot")
    use_snapshot = mode == "snapshot"
    force_rebuild = getattr(args, "force_rebuild", False)
    distributed = getattr(args, "distributed", False)
    redis_host = getattr(args, "redis_host", "localhost")

    build_mode = f"{mode}-mode"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    exec_mode = f", distributed (redis={redis_host})" if distributed else ", local"
    logger.info(
        f"Running coverage: {len(paths)} benchmark(s), "
        f"{build_mode}, {rebuild_mode}{exec_mode}"
    )

    # Create full job DAG with coverage enabled
    from crsbench.benchmark_ci.cli.commands.all_cmd import _build_dag

    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_snapshot,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
        inc_coverage=True,
    )

    # Filter to build + coverage jobs only
    build_jobs = [
        j
        for j in all_jobs
        if isinstance(j, (BuildSingleVariantJob, PrepareIncImageJob))
    ]
    coverage_jobs = [j for j in all_jobs if isinstance(j, FlatCollectCoverageJob)]

    logger.info(
        f"Jobs: {len(build_jobs)} build, {len(coverage_jobs)} coverage "
        f"(filtered from {len(all_jobs)} total)"
    )

    output_dir = getattr(args, "output_dir", None)

    start_dt = datetime.now()

    if distributed:
        from crsbench.benchmark_ci.cli.commands.build_cmd import (
            _run_distributed_build,
        )

        build_results = _run_distributed_build(
            build_jobs, redis_host, output_dir=output_dir
        )

        from crsbench.distributed.ci_jobs import (
            ci_results_to_executor_results,
            enqueue_and_poll_ci_jobs,
        )

        if coverage_jobs:
            verify_queue_name = "crsbench_ci_verify"
            raw_coverage_results = enqueue_and_poll_ci_jobs(
                coverage_jobs,
                redis_host,
                queue_name=verify_queue_name,
                output_dir=output_dir,
            )
            coverage_results = ci_results_to_executor_results(raw_coverage_results)
        else:
            coverage_results = {}

        dag_results = {**build_results, **coverage_results}
    else:
        from crsbench.benchmark_ci.executor import execute_jobs_locally

        relevant_jobs = build_jobs + coverage_jobs
        dag_results = execute_jobs_locally(
            relevant_jobs,
            output_dir=Path(output_dir) if output_dir else None,
        )

    # Build summary from results
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)

    for (
        path,
        _supports_inc,
        _rts_mode,
        _cpv_ids,
        _patch_keys,
        build_job_ids,
    ) in benchmark_metadata:
        coverage_result = aggregate_coverage_result(dag_results, path.name)

        # Collect build time from BuildSingleVariantJob results
        shared_build = 0.0
        storage_bytes = 0
        for job_id in build_job_ids:
            br = dag_results.get(job_id)
            if br:
                shared_build += br.elapsed_seconds
                if br.job_result:
                    storage_bytes = max(
                        storage_bytes,
                        br.job_result.details.get("storage_bytes", 0),
                    )

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
