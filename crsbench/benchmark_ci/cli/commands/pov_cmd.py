"""POV verification subcommand.

Uses _build_dag() from all_cmd to create jobs, then executes via Redis:
Phase 1 — Build jobs via VariantPlanner + Redis build queue
Phase 2 — VerifyCpvPovJob/VerifyCpvVarJob via Redis verify queue
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
    build_jobs = [
        j
        for j in all_jobs
        if isinstance(j, (BuildSingleVariantJob, PrepareIncImageJob))
    ]
    pov_jobs = [
        j for j in all_jobs if isinstance(j, (VerifyCpvPovJob, VerifyCpvVarJob))
    ]

    logger.info(
        f"Jobs: {len(build_jobs)} build, {len(pov_jobs)} verify "
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

        if pov_jobs:
            verify_queue_name = "crsbench_ci_verify"
            raw_verify_results = enqueue_and_poll_ci_jobs(
                pov_jobs,
                redis_host,
                queue_name=verify_queue_name,
                output_dir=output_dir,
            )
            verify_results = ci_results_to_executor_results(raw_verify_results)
        else:
            verify_results = {}

        dag_results = {**build_results, **verify_results}
    else:
        from crsbench.benchmark_ci.executor import execute_jobs_locally

        relevant_jobs = build_jobs + pov_jobs
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

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
