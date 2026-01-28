"""Regression test selection check subcommand.

Same structure as patch_cmd but uses test_mode="RTS" for PatchVariantTestJob.
Skips benchmarks without rts_mode configured.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.cli.benchmark_discovery import (
    discover_cpv_ids,
    discover_harness_names,
    discover_patch_paths,
    discover_pov_paths,
)
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_patch_results
from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildVariantsJob,
    PatchVariantTestJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
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
    """Run regression test selection checks via flat DAG."""
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
        f"Running rts: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}, {rebuild_mode}"
    )

    # Build flat DAG across all benchmarks
    all_jobs: list[Job] = []
    benchmark_metadata: list[tuple[Path, bool, str | None, list[tuple[str, str]]]] = []

    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_name = path.name

        # Skip benchmarks without RTS mode
        if not rts_mode:
            benchmark_metadata.append((path, supports_inc, rts_mode, []))
            continue

        build_job = BuildVariantsJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            use_inc_build=effective_inc,
            force_rebuild=force_rebuild,
            source_mode=source_mode,
        )
        all_jobs.append(build_job)

        # Discover patches and create per-patch build + RTS test jobs
        patch_keys: list[tuple[str, str]] = []
        harnesses = discover_harness_names(path)
        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                patches = discover_patch_paths(path, harness, cpv_id)
                pov_paths = discover_pov_paths(path, harness, cpv_id)

                for patch_id, patch_path in patches:
                    patch_keys.append((cpv_id, patch_id))

                    build_patch_job = BuildPatchVariantJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        patch_path=patch_path,
                        harness=harness,  # Pass harness for per-harness sanitizer
                        use_inc_build=effective_inc,
                        force_rebuild=force_rebuild,
                        build_job_id=build_job.job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(build_patch_job)

                    test_job = PatchVariantTestJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        harness=harness,
                        pov_paths=pov_paths,
                        test_mode="RTS",
                        build_patch_job_id=build_patch_job.job_id,
                    )
                    all_jobs.append(test_job)

        benchmark_metadata.append((path, supports_inc, rts_mode, patch_keys))

    # Log DAG summary
    build_count = sum(1 for j in all_jobs if isinstance(j, BuildVariantsJob))
    patch_build_count = sum(1 for j in all_jobs if isinstance(j, BuildPatchVariantJob))
    patch_test_count = sum(1 for j in all_jobs if isinstance(j, PatchVariantTestJob))
    logger.info(
        f"DAG: {len(all_jobs)} jobs — "
        f"{build_count} build, {patch_build_count} patch-build, "
        f"{patch_test_count} patch-test (RTS)"
    )

    # Execute with typed concurrency
    start_dt = datetime.now()
    if all_jobs:
        executor = DAGExecutor(
            type_limits={"build": build_workers, "verify": verify_workers}
        )
        output_dir = getattr(args, "output_dir", None)
        context = JobContext(output_dir=Path(output_dir) if output_dir else None)
        dag_results = executor.execute(all_jobs, context)
    else:
        dag_results = {}

    # Aggregate into ValidationSummary
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)

    for path, supports_inc, rts_mode, patch_keys in benchmark_metadata:
        if not rts_mode:
            patch_rts_result = CheckResult.skip("No RTS mode configured")
        else:
            patch_rts_result = aggregate_patch_results(
                dag_results, path.name, patch_keys, test_mode="RTS"
            )

        build_result = dag_results.get(f"build-variants:{path.name}")
        shared_build = build_result.elapsed_seconds if build_result else 0.0
        storage_bytes = 0
        if build_result and build_result.job_result:
            storage_bytes = build_result.job_result.details.get("storage_bytes", 0)
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
