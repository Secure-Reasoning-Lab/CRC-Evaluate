"""Combined CI validation subcommand (all checks).

Constructs a flat DAG with ONE shared BuildVariantsJob per benchmark,
fan-out to POV/patch/RTS/coverage verify jobs. Eliminates redundant
builds when running all check types together.
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
from crsbench.benchmark_ci.cli.commands.format_cmd import (
    _validate_benchmark as validate_format,
)
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import (
    aggregate_coverage_result,
    aggregate_patch_results,
    aggregate_pov_results,
)
from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildVariantsJob,
    FlatCollectCoverageJob,
    TestPatchVariantJob,
    VerifyCpvPovJob,
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

# Type alias for benchmark metadata tuple
type BenchmarkMeta = tuple[Path, bool, str | None, list[str], list[tuple[str, str]]]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the all subcommand."""
    parser = subparsers.add_parser(
        "all",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run all CI checks (format, POV, patch, RTS, coverage)",
    )
    parser.set_defaults(ci_func=run_all)


def _build_dag(
    paths: list[Path],
    *,
    use_inc_build: bool,
    force_rebuild: bool,
    source_mode: str,
) -> tuple[list[Job], list[BenchmarkMeta]]:
    """Build flat DAG with shared BuildVariantsJob per benchmark.

    Returns:
        Tuple of (all_jobs, benchmark_metadata)
    """
    all_jobs: list[Job] = []
    benchmark_metadata: list[BenchmarkMeta] = []

    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_name = path.name

        # ONE shared build job per benchmark
        build_job = BuildVariantsJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            use_inc_build=effective_inc,
            force_rebuild=force_rebuild,
            source_mode=source_mode,
        )
        all_jobs.append(build_job)

        harnesses = discover_harness_names(path)
        cpv_ids: list[str] = []
        patch_keys: list[tuple[str, str]] = []

        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                if cpv_id in cpv_ids:
                    continue
                cpv_ids.append(cpv_id)

                # POV verify jobs
                pov_paths = discover_pov_paths(path, harness, cpv_id)
                if pov_paths:
                    pov_job = VerifyCpvPovJob(
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        harness=harness,
                        pov_paths=pov_paths,
                        build_job_id=build_job.job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(pov_job)

                # Patch build + test jobs (FULL mode)
                patches = discover_patch_paths(path, harness, cpv_id)
                for patch_id, patch_path in patches:
                    patch_keys.append((cpv_id, patch_id))

                    build_patch_job = BuildPatchVariantJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        patch_path=patch_path,
                        use_inc_build=effective_inc,
                        force_rebuild=force_rebuild,
                        build_job_id=build_job.job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(build_patch_job)

                    test_full_job = TestPatchVariantJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        harness=harness,
                        pov_paths=pov_paths,
                        test_mode="FULL",
                        build_patch_job_id=build_patch_job.job_id,
                    )
                    all_jobs.append(test_full_job)

                    # RTS test jobs (if RTS mode available)
                    if rts_mode:
                        test_rts_job = TestPatchVariantJob(
                            benchmark_path=path,
                            benchmark_name=benchmark_name,
                            cpv_id=cpv_id,
                            patch_id=patch_id,
                            harness=harness,
                            pov_paths=pov_paths,
                            test_mode="RTS",
                            build_patch_job_id=build_patch_job.job_id,
                        )
                        all_jobs.append(test_rts_job)

        # Coverage job
        harness = harnesses[0] if harnesses else ""
        coverage_job = FlatCollectCoverageJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            harness=harness,
            build_job_id=build_job.job_id,
        )
        all_jobs.append(coverage_job)

        benchmark_metadata.append((path, supports_inc, rts_mode, cpv_ids, patch_keys))

    return all_jobs, benchmark_metadata


def _log_dag_summary(all_jobs: list[Job]) -> None:
    """Log DAG job counts by type."""
    build_count = sum(1 for j in all_jobs if isinstance(j, BuildVariantsJob))
    pov_jobs = [j for j in all_jobs if isinstance(j, VerifyCpvPovJob)]
    pov_blob_count = sum(len(j.pov_paths) for j in pov_jobs)
    patch_build_count = sum(1 for j in all_jobs if isinstance(j, BuildPatchVariantJob))
    patch_test_count = sum(1 for j in all_jobs if isinstance(j, TestPatchVariantJob))
    coverage_count = sum(1 for j in all_jobs if isinstance(j, FlatCollectCoverageJob))
    logger.info(
        f"DAG: {len(all_jobs)} jobs — "
        f"{build_count} build, {len(pov_jobs)} pov-verify ({pov_blob_count} blobs), "
        f"{patch_build_count} patch-build, {patch_test_count} patch-test, "
        f"{coverage_count} coverage"
    )


def _aggregate_benchmark(
    dag_results: dict,
    path: Path,
    supports_inc: bool,
    rts_mode: str | None,
    cpv_ids: list[str],
    patch_keys: list[tuple[str, str]],
    format_results: dict[str, CheckResult],
    start_dt: datetime,
) -> BenchmarkValidationResult:
    """Aggregate DAG results into a single BenchmarkValidationResult."""
    benchmark_name = path.name

    fmt_result = format_results.get(
        benchmark_name, CheckResult.make_error("format check not run")
    )
    pov_result = aggregate_pov_results(dag_results, benchmark_name, cpv_ids)
    patch_result = aggregate_patch_results(
        dag_results, benchmark_name, patch_keys, test_mode="FULL"
    )

    if rts_mode:
        rts_result = aggregate_patch_results(
            dag_results, benchmark_name, patch_keys, test_mode="RTS"
        )
    else:
        rts_result = CheckResult.skip("No RTS mode")

    coverage_result = aggregate_coverage_result(dag_results, benchmark_name)

    # Get shared build time from BuildVariantsJob
    build_job_id = f"build-variants:{benchmark_name}"
    build_result = dag_results.get(build_job_id)
    shared_build_time = build_result.elapsed_seconds if build_result else 0.0

    return BenchmarkValidationResult(
        benchmark=benchmark_name,
        benchmark_path=path,
        format_check=fmt_result,
        pov_check=pov_result,
        patch_check=patch_result,
        patch_rts_check=rts_result,
        coverage_check=coverage_result,
        shared_build_time=shared_build_time,
        supports_inc_build=supports_inc,
        rts_mode=rts_mode,
        started_at=start_dt,
        finished_at=datetime.now(),
    )


def run_all(args: argparse.Namespace) -> int:
    """Run all checks on resolved benchmarks with shared build via flat DAG."""
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
        f"Running all: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}, {rebuild_mode}"
    )

    start_dt = datetime.now()

    # Phase 1: Format validation (fast, no DAG needed)
    format_results: dict[str, CheckResult] = {}
    for path in paths:
        fmt_result = validate_format(path, source_mode)
        format_results[path.name] = fmt_result.format_check or CheckResult.make_error(
            "format check not run"
        )

    # Phase 2: Build and execute flat DAG
    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
    )
    _log_dag_summary(all_jobs)

    output_dir = getattr(args, "output_dir", None)
    context = JobContext(output_dir=Path(output_dir) if output_dir else None)
    executor = DAGExecutor(
        type_limits={"build": build_workers, "verify": verify_workers}
    )
    dag_results = executor.execute(all_jobs, context)

    # Phase 3: Aggregate into ValidationSummary
    summary = ValidationSummary(started_at=start_dt, check_mode=CheckMode.ALL)
    for path, supports_inc, rts_mode, cpv_ids, patch_keys in benchmark_metadata:
        summary.add_result(
            _aggregate_benchmark(
                dag_results,
                path,
                supports_inc,
                rts_mode,
                cpv_ids,
                patch_keys,
                format_results,
                start_dt,
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
