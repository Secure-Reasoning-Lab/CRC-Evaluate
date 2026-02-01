"""Build-only CI subcommand.

Builds POV variants (vulnerable, allpatched, CPV) without patch variants
or verification. Default behavior: always run Docker build (no cleanup,
no skip), fully utilizing Docker cache layers.

Use --force-rebuild to clean up before building.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.benchmark_ci.cli.benchmark_discovery import (
    discover_cpv_ids,
    discover_harness_names,
)
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.cli.result_aggregator import aggregate_pov_build_results
from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.executor import DAGExecutor
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)

# Type alias for build command metadata
type BuildBenchmarkMeta = tuple[Path, bool, str | None]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the build subcommand."""
    parser = subparsers.add_parser(
        "build",
        parents=[
            create_benchmark_selection_parent(),
            create_output_options_parent(),
        ],
        help="Build POV variants only (no verification, no patch builds)",
    )
    parser.add_argument(
        "--build-workers",
        type=int,
        default=4,
        help="Number of parallel build workers (default: 4)",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        default=False,
        help="Clean up before building (default: False, relies on Docker cache)",
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["pkgs", "main_repo"],
        default="main_repo",
        help="Source mode: 'pkgs' (bundled tarballs) or 'main_repo' (git clone, default)",
    )
    parser.add_argument(
        "--no-inc-build",
        action="store_true",
        help="Force full build instead of incremental build (default uses inc-build)",
    )
    parser.set_defaults(ci_func=run_build)


def _build_only_dag(
    paths: list[Path],
    *,
    use_inc_build: bool,
    force_rebuild: bool,
    source_mode: str,
) -> tuple[list[Job], list[BuildBenchmarkMeta]]:
    """Build flat DAG with only BuildSingleVariantJob instances.

    Creates build jobs for vulnerable, allpatched, and CPV variants
    per sanitizer. No verify jobs, no patch jobs, no coverage.

    All jobs have skip_if_cached=False so Docker build always runs
    (relying on Docker layer cache for speed).

    Returns:
        Tuple of (all_jobs, benchmark_metadata)
    """
    from crsbench.benchmark_ci.cli.commands.all_cmd import _load_benchmark_adapter
    from crsbench.builder.infrastructure import OSSFuzzInfrastructure

    all_jobs: list[Job] = []
    benchmark_metadata: list[BuildBenchmarkMeta] = []

    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_name = path.name

        adapter = _load_benchmark_adapter(path, source_mode)
        if not adapter:
            logger.warning(f"Failed to load adapter for {benchmark_name}, skipping")
            continue

        ref_commit = adapter.get_ref_commit()
        base_commit = adapter.get_base_commit()

        if ref_commit:
            mode = BenchmarkMode.DELTA
            commit = ref_commit
        elif base_commit:
            mode = BenchmarkMode.FULL
            commit = base_commit
        else:
            logger.warning(f"No commit found for {benchmark_name}, skipping")
            continue

        main_repo = adapter.main_repo
        language = adapter.lang
        repo_name = adapter.repo_name

        required_sanitizers = adapter.get_all_cpv_sanitizers()
        infra = OSSFuzzInfrastructure(Path("oss-fuzz"))
        all_patches = infra.get_all_patches(path)

        is_delta = mode == BenchmarkMode.DELTA
        vulnerable_variant_type = (
            VariantType.DELTA_REF if is_delta else VariantType.FULL_BASE
        )

        for sanitizer in required_sanitizers:
            # Vulnerable variant
            all_jobs.append(
                BuildSingleVariantJob(
                    benchmark_path=path,
                    benchmark_name=benchmark_name,
                    variant_type=vulnerable_variant_type,
                    commit=commit,
                    main_repo=main_repo,
                    mode=mode,
                    language=language,
                    use_inc_build=effective_inc,
                    force_rebuild=force_rebuild,
                    skip_if_cached=False,
                    source_mode=source_mode,
                    sanitizer=sanitizer,
                    repo_name=repo_name,
                )
            )

            # Allpatched variant
            all_jobs.append(
                BuildSingleVariantJob(
                    benchmark_path=path,
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.ALL_PATCHED,
                    commit=commit,
                    main_repo=main_repo,
                    mode=mode,
                    language=language,
                    patches=all_patches,
                    use_inc_build=effective_inc,
                    force_rebuild=force_rebuild,
                    skip_if_cached=False,
                    source_mode=source_mode,
                    sanitizer=sanitizer,
                    repo_name=repo_name,
                )
            )

        # CPV variants
        harnesses = discover_harness_names(path)
        seen_cpvs: set[str] = set()

        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                if cpv_id in seen_cpvs:
                    continue
                seen_cpvs.add(cpv_id)

                cpv_num = int(cpv_id.split("_")[1])
                cpv_sanitizer = adapter.get_cpv_sanitizer(harness, cpv_id)
                cpv_variant_patches = infra.get_patches_except(path, cpv_num)

                all_jobs.append(
                    BuildSingleVariantJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        variant_type=VariantType.CPV,
                        commit=commit,
                        main_repo=main_repo,
                        mode=mode,
                        language=language,
                        cpv_num=cpv_num,
                        patches=cpv_variant_patches,
                        use_inc_build=effective_inc,
                        force_rebuild=force_rebuild,
                        skip_if_cached=False,
                        source_mode=source_mode,
                        sanitizer=cpv_sanitizer,
                        repo_name=repo_name,
                    )
                )

        benchmark_metadata.append((path, supports_inc, rts_mode))

    return all_jobs, benchmark_metadata


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
        if not job_id.startswith(f"build-single:{benchmark_name}:"):
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


def run_build(args: argparse.Namespace) -> int:
    """Run build-only checks on resolved benchmarks."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    source_mode = getattr(args, "source", "main_repo")
    build_workers = getattr(args, "build_workers", 4)
    use_inc_build = not getattr(args, "no_inc_build", False)
    force_rebuild = getattr(args, "force_rebuild", False)

    build_mode = "inc-build" if use_inc_build else "full-build"
    rebuild_mode = "force-rebuild" if force_rebuild else "docker-cache"
    logger.info(
        f"Building: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, {build_mode}, {rebuild_mode}"
    )

    start_dt = datetime.now()

    all_jobs, benchmark_metadata = _build_only_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
    )

    logger.info(f"DAG: {len(all_jobs)} build jobs")

    output_dir = getattr(args, "output_dir", None)
    output_path = Path(output_dir) if output_dir else None
    context = JobContext(output_dir=output_path)
    executor = DAGExecutor(type_limits={"build": build_workers})
    dag_results = executor.execute(all_jobs, context)

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
