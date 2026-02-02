"""Regression test selection check subcommand.

Uses _build_dag() from all_cmd to create jobs, then executes via Redis.
Same structure as patch_cmd but uses test_mode="RTS" for PatchUnitTestJob.
Skips benchmarks without rts_mode configured.
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
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_patch_results
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildSingleVariantJob,
    PatchUnitTestJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

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
    """Run regression test selection checks via Redis."""
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
    distributed = getattr(args, "distributed", False)
    redis_host = getattr(args, "redis_host", "localhost")

    build_mode = "inc-build" if use_inc_build else "full-build"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    exec_mode = f", distributed (redis={redis_host})" if distributed else ", local"
    logger.info(
        f"Running rts: {len(paths)} benchmark(s), "
        f"{build_mode}, {rebuild_mode}{exec_mode}"
    )

    # Create full job DAG (reuses all_cmd infrastructure)
    from crsbench.benchmark_ci.cli.commands.all_cmd import _build_dag

    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
    )

    # Filter to build + RTS-relevant jobs
    # RTS needs: BuildSingleVariantJob (base builds), BuildPatchVariantJob (patch builds),
    # and PatchUnitTestJob with test_mode="RTS"
    build_jobs = [j for j in all_jobs if isinstance(j, BuildSingleVariantJob)]
    rts_jobs = [
        j
        for j in all_jobs
        if isinstance(j, BuildPatchVariantJob)
        or (isinstance(j, PatchUnitTestJob) and j.test_mode == "RTS")
    ]

    logger.info(
        f"Jobs: {len(build_jobs)} build, {len(rts_jobs)} RTS "
        f"(filtered from {len(all_jobs)} total)"
    )

    start_dt = datetime.now()

    if distributed:
        dag_results: dict = {}

        if build_jobs:
            from crsbench.benchmark_ci.cli.commands.build_cmd import (
                _run_distributed_build,
            )

            dag_results = _run_distributed_build(build_jobs, redis_host)

        from crsbench.distributed.ci_jobs import (
            ci_results_to_executor_results,
            enqueue_and_poll_ci_jobs,
        )

        if rts_jobs:
            verify_queue_name = f"crsbench_ci_{redis_host}_verify"
            raw_rts_results = enqueue_and_poll_ci_jobs(
                rts_jobs, redis_host, queue_name=verify_queue_name
            )
            rts_results = ci_results_to_executor_results(raw_rts_results)
            dag_results = {**dag_results, **rts_results}
    else:
        from crsbench.benchmark_ci.executor import execute_jobs_locally

        relevant_jobs = build_jobs + rts_jobs
        dag_results = execute_jobs_locally(relevant_jobs)

    # Aggregate into ValidationSummary
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)

    for (
        path,
        supports_inc,
        rts_mode,
        _cpv_ids,
        patch_keys,
        build_job_ids,
    ) in benchmark_metadata:
        if not rts_mode:
            patch_rts_result = CheckResult.skip("No RTS mode configured")
        else:
            patch_rts_result = aggregate_patch_results(
                dag_results, path.name, patch_keys, test_mode="RTS"
            )

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
                patch_rts_check=patch_rts_result,
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
