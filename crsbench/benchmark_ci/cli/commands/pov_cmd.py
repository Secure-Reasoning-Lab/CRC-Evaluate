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
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_pov_results
from crsbench.benchmark_ci.jobs.flat import (
    BuildSingleVariantJob,
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
    use_inc_build = not getattr(args, "no_inc_build", False)
    force_rebuild = getattr(args, "force_rebuild", True)
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)
    redis_host = getattr(args, "redis_host", "localhost")

    build_mode = "inc-build" if use_inc_build else "full-build"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    logger.info(
        f"Running pov: {len(paths)} benchmark(s), "
        f"{build_mode}, {rebuild_mode}, redis={redis_host}"
    )

    # Create full job DAG (reuses all_cmd infrastructure)
    from crsbench.benchmark_ci.cli.commands.all_cmd import _build_dag

    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
    )

    # Filter to build + POV verify jobs only
    build_jobs = [j for j in all_jobs if isinstance(j, BuildSingleVariantJob)]
    pov_jobs = [
        j for j in all_jobs if isinstance(j, (VerifyCpvPovJob, VerifyCpvVarJob))
    ]

    logger.info(
        f"Jobs: {len(build_jobs)} build, {len(pov_jobs)} verify "
        f"(filtered from {len(all_jobs)} total)"
    )

    # Phase 1: Builds via Redis
    start_dt = datetime.now()
    from crsbench.benchmark_ci.cli.commands.build_cmd import _run_distributed_build

    build_results = _run_distributed_build(build_jobs, redis_host)

    # Phase 2: POV verify via Redis
    from crsbench.distributed.ci_jobs import (
        ci_results_to_executor_results,
        enqueue_and_poll_ci_jobs,
    )

    if pov_jobs:
        verify_queue_name = f"crsbench_ci_{redis_host}_verify"
        raw_verify_results = enqueue_and_poll_ci_jobs(
            pov_jobs, redis_host, queue_name=verify_queue_name
        )
        verify_results = ci_results_to_executor_results(raw_verify_results)
    else:
        verify_results = {}

    dag_results = {**build_results, **verify_results}

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
        pov_result = aggregate_pov_results(dag_results, path.name, cpv_ids)

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
                pov_check=pov_result,
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
