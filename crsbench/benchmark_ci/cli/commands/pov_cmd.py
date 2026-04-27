"""POV verification subcommand.

Uses _build_dag() from all_cmd to create jobs. In distributed mode, build
and verify jobs are enqueued together in a single batch with per-job queue
routing; RQ honors the depends_on graph so verify jobs start as soon as their
build dependencies finish (build/verify pipelining).
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
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import (
    aggregate_pov_build_results,
    aggregate_pov_pov_results,
    aggregate_pov_var_results,
)
from crsbench.benchmark_ci.jobs.flat import (
    BuildSingleVariantJob,
    PrepareIncImageJob,
    VerifyCpvPovJob,
    VerifyCpvVarJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.benchmark_ci.jobs.base import Job

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the pov subcommand."""
    parser = subparsers.add_parser(
        "pov",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run POV verification",
    )
    parser.add_argument(
        "--delete-failed-povs",
        action="store_true",
        dest="delete_failed_povs",
        help=(
            "After verification, prompt to delete variant POV blobs/logs "
            "(pov_1+) whose verdict is not 'cpv'. pov_0 is never deleted."
        ),
    )
    parser.set_defaults(ci_func=run_pov)


def run_pov(args: argparse.Namespace) -> int:
    """Run POV verification on resolved benchmarks via Redis."""
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
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)
    distributed = getattr(args, "distributed", False)
    redis_host = getattr(args, "redis_host", "localhost")

    build_mode = f"{mode}-mode"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    exec_mode = f", distributed (redis={redis_host})" if distributed else ", local"
    logger.info(
        f"Running pov: {len(paths)} benchmark(s), "
        f"{build_mode}, {rebuild_mode}{exec_mode}"
    )

    # Create full job DAG (reuses all_cmd infrastructure)
    from crsbench.benchmark_ci.cli.commands.all_cmd import _build_dag

    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_snapshot,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
    )

    # Filter to build + POV verify jobs only
    relevant_job_types = (
        BuildSingleVariantJob,
        PrepareIncImageJob,
        VerifyCpvPovJob,
        VerifyCpvVarJob,
    )
    relevant_jobs = [j for j in all_jobs if isinstance(j, relevant_job_types)]
    n_build = sum(1 for j in relevant_jobs if j.job_type == "build")
    n_verify = len(relevant_jobs) - n_build

    logger.info(
        f"Jobs: {n_build} build, {n_verify} verify "
        f"(filtered from {len(all_jobs)} total)"
    )

    output_dir = getattr(args, "output_dir", None)

    start_dt = datetime.now()

    if distributed:
        from crsbench.distributed.ci_jobs import (
            ci_results_to_executor_results,
            enqueue_and_poll_ci_jobs,
        )

        def route_job(job: Job) -> str:
            return (
                "crsbench_ci_build" if job.job_type == "build" else "crsbench_ci_verify"
            )

        raw_results = enqueue_and_poll_ci_jobs(
            relevant_jobs,
            redis_host,
            queue_name=route_job,
            output_dir=output_dir,
        )
        dag_results = ci_results_to_executor_results(raw_results)
    else:
        from crsbench.benchmark_ci.executor import execute_jobs_locally

        dag_results = execute_jobs_locally(
            relevant_jobs,
            output_dir=Path(output_dir) if output_dir else None,
        )

    # Aggregate into ValidationSummary
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)

    for (
        path,
        supports_inc,
        rts_mode,
        cpv_ids,
        _patch_keys,
        build_job_ids,
    ) in benchmark_metadata:
        pov_build = aggregate_pov_build_results(dag_results, path.name)
        pov_pov = aggregate_pov_pov_results(dag_results, path.name, cpv_ids)
        pov_var = aggregate_pov_var_results(dag_results, path.name, cpv_ids)

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
                pov_build_check=pov_build,
                pov_pov_check=pov_pov,
                pov_var_check=pov_var,
                shared_build_time=shared_build,
                storage_bytes=storage_bytes,
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

    if output_dir:
        save_output_dir(summary, Path(output_dir), check_mode=CheckMode.ALL)

    output_path = getattr(args, "output", None)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary.to_dict(), indent=2))

    if getattr(args, "delete_failed_povs", False):
        from crsbench.benchmark_ci.cli.pov_cleanup import (
            prompt_and_delete_failed_povs,
        )

        prompt_and_delete_failed_povs(relevant_jobs, dag_results)

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
