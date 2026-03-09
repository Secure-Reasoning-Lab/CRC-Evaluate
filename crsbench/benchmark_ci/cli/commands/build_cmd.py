"""Build-only CI subcommand.

Builds POV variants (vulnerable, allpatched, CPV) without patch variants
or verification. Uses VariantPlanner for build job creation and the
unified enqueue_and_poll_ci_jobs() for distributed execution.

Use --force-rebuild to clean up before building.
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
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_pov_build_results
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

if TYPE_CHECKING:
    import argparse

    from crsbench.executor.types import ExecutorResult

logger = get_logger(__name__)

# Type alias for build command metadata
type BuildBenchmarkMeta = tuple[Path, bool, str | None]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the build subcommand."""
    parser = subparsers.add_parser(
        "build",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Build POV variants only (no verification, no patch builds)",
    )
    # Override: build relies on Docker cache by default
    parser.set_defaults(force_rebuild=False)
    parser.set_defaults(ci_func=run_build)


def _aggregate_build(
    dag_results: dict,
    path: Path,
    supports_inc: bool,
    rts_mode: str | None,
    start_dt: datetime,
) -> BenchmarkValidationResult:
    """Aggregate DAG results into a build-only BenchmarkValidationResult."""
    benchmark_name = path.name

    pov_build_result = aggregate_pov_build_results(dag_results, benchmark_name)

    # Collect storage from build results
    storage_bytes = 0
    for job_id, result in dag_results.items():
        if not job_id.startswith(f"build-single/{benchmark_name}/"):
            continue
        if result.job_result:
            storage_bytes = max(
                storage_bytes,
                result.job_result.details.get("storage_bytes", 0),
            )

    return BenchmarkValidationResult(
        benchmark=benchmark_name,
        benchmark_path=path,
        pov_build_check=pov_build_result,
        shared_build_time=pov_build_result.build_time,
        storage_bytes=storage_bytes,
        supports_inc_build=supports_inc,
        rts_mode=rts_mode,
        started_at=start_dt,
        finished_at=datetime.now(),
    )


def _run_distributed_build(
    all_jobs: list,
    redis_host: str,
    queue_name: str = "crsbench_ci_build",
    output_dir: str | None = None,
) -> dict[str, "ExecutorResult"]:
    """Enqueue build jobs to Redis and return ExecutorResult dict.

    Uses the unified enqueue_and_poll_ci_jobs path for all job types.

    Args:
        all_jobs: List of BuildSingleVariantJob instances
        redis_host: Redis server hostname
        queue_name: Redis queue name
        output_dir: Optional directory for per-job logs on worker

    Returns:
        Dict mapping job_id to ExecutorResult
    """
    from crsbench.distributed.ci_jobs import (
        ci_results_to_executor_results,
        enqueue_and_poll_ci_jobs,
    )

    raw_results = enqueue_and_poll_ci_jobs(
        all_jobs, redis_host, queue_name, output_dir=output_dir
    )
    return ci_results_to_executor_results(raw_results)


def run_build(args: argparse.Namespace) -> int:
    """Run build-only checks on resolved benchmarks."""
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
    rebuild_mode = "force-rebuild" if force_rebuild else "docker-cache"
    exec_mode = f", distributed (redis={redis_host})" if distributed else ", local"
    logger.info(
        f"Building: {len(paths)} benchmark(s), {build_mode}, {rebuild_mode}{exec_mode}"
    )

    start_dt = datetime.now()

    # Use VariantPlanner for build job creation
    from crsbench.executor.variant_planner import VariantPlanner

    planner = VariantPlanner(
        oss_fuzz_path=Path(ensure_oss_fuzz_root()), source_mode=source_mode
    )
    build_jobs = planner.plan_all_builds(
        list(paths),
        use_inc_build=use_snapshot,
        force_rebuild=force_rebuild,
        skip_if_cached=False,
    )

    # Collect benchmark metadata for aggregation
    benchmark_metadata: list[BuildBenchmarkMeta] = []
    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        benchmark_metadata.append((path, supports_inc, rts_mode))

    logger.info(f"VariantPlanner: {len(build_jobs)} build jobs")

    output_dir = getattr(args, "output_dir", None)

    if distributed:
        dag_results = _run_distributed_build(
            build_jobs, redis_host, output_dir=output_dir
        )
    else:
        from crsbench.benchmark_ci.executor import execute_jobs_locally

        dag_results = execute_jobs_locally(
            build_jobs,
            output_dir=Path(output_dir) if output_dir else None,
        )

    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.BUILD)
    for path, supports_inc, rts_mode in benchmark_metadata:
        summary.add_result(
            _aggregate_build(dag_results, path, supports_inc, rts_mode, start_dt)
        )

    summary.finished_at = datetime.now()

    print_results_table(
        summary,
        check_mode=CheckMode.BUILD,
        no_color=getattr(args, "no_color", False),
    )

    if output_dir:
        save_output_dir(summary, Path(output_dir), check_mode=CheckMode.BUILD)

    output_json = getattr(args, "output", None)
    if output_json:
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary.to_dict(), indent=2))

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
