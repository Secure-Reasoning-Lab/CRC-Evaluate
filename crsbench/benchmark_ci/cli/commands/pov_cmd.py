"""POV verification subcommand.

Constructs a flat DAG of BuildVariantsJob + VerifyCpvPovJob per CPV,
executed with typed concurrency limits.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.cli.benchmark_discovery import (
    discover_cpv_ids,
    discover_harness_names,
    discover_pov_paths,
)
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_pov_results
from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import BuildVariantsJob, VerifyCpvPovJob
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
    """Run POV verification on resolved benchmarks via flat DAG."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
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
        f"Running pov: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}, {rebuild_mode}"
    )

    # Build flat DAG across all benchmarks
    all_jobs: list[Job] = []
    benchmark_metadata: list[tuple[Path, bool, str | None, list[str]]] = []

    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
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

        # Discover CPVs and create per-CPV verify jobs
        harnesses = discover_harness_names(path)
        cpv_ids: list[str] = []
        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                if cpv_id in cpv_ids:
                    continue
                cpv_ids.append(cpv_id)
                pov_paths = discover_pov_paths(path, harness, cpv_id)
                if not pov_paths:
                    continue
                verify_job = VerifyCpvPovJob(
                    benchmark_name=benchmark_name,
                    cpv_id=cpv_id,
                    harness=harness,
                    pov_paths=pov_paths,
                    build_job_id=build_job.job_id,
                )
                all_jobs.append(verify_job)

        benchmark_metadata.append((path, supports_inc, rts_mode, cpv_ids))

    # Execute with typed concurrency
    start_dt = datetime.now()
    executor = DAGExecutor(
        type_limits={"build": build_workers, "verify": verify_workers}
    )
    output_dir = getattr(args, "output_dir", None)
    context = JobContext(output_dir=Path(output_dir) if output_dir else None)
    dag_results = executor.execute(all_jobs, context)

    # Aggregate into ValidationSummary
    summary = ValidationSummary(started_at=start_dt)

    for path, supports_inc, rts_mode, cpv_ids in benchmark_metadata:
        pov_result = aggregate_pov_results(dag_results, path.name, cpv_ids)
        summary.add_result(
            BenchmarkValidationResult(
                benchmark=path.name,
                benchmark_path=path,
                pov_check=pov_result,
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
