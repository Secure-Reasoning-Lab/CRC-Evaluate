"""Combined CI validation subcommand (all checks).

Constructs a flat DAG with per-variant BuildSingleVariantJob instances,
fan-out to POV/patch/RTS/coverage verify jobs. Enables parallel builds
across variants when workers are available.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
    BuildSingleVariantJob,
    BuildVariantsJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
    VerifyCpvPovJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.executor import DAGExecutor
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)

# Type alias for benchmark metadata tuple:
# path, supports_inc, rts_mode, cpv_ids, patch_keys, build_job_ids
type BenchmarkMeta = tuple[
    Path, bool, str | None, list[str], list[tuple[str, str]], list[str]
]


def _load_benchmark_adapter(path: Path, source_mode: str) -> MetaYamlAdapter | None:
    """Load benchmark adapter for extracting build configuration."""
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import get_oss_fuzz_root

    oss_fuzz_path = Path(get_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    return engine._load_adapter(path)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the all subcommand."""
    parser = subparsers.add_parser(
        "all",
        parents=[
            create_benchmark_selection_parent(),
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Run all CI checks (format, POV, patch, RTS; coverage with --inc-coverage)",
    )
    parser.add_argument(
        "--inc-coverage",
        action="store_true",
        help="Include coverage build and verification (expensive, disabled by default)",
    )
    parser.set_defaults(ci_func=run_all)


def _build_dag(
    paths: list[Path],
    *,
    use_inc_build: bool,
    force_rebuild: bool,
    source_mode: str,
    max_povs_per_cpv: int | None = None,
    inc_coverage: bool = False,
) -> tuple[list[Job], list[BenchmarkMeta]]:
    """Build flat DAG with per-variant BuildSingleVariantJob instances.

    Creates separate build jobs for vulnerable and allpatched variants,
    enabling parallel builds across variants when workers are available.

    Returns:
        Tuple of (all_jobs, benchmark_metadata)
    """
    all_jobs: list[Job] = []
    benchmark_metadata: list[BenchmarkMeta] = []

    for path in paths:
        supports_inc, rts_mode = _load_project_capabilities(path)
        effective_inc = use_inc_build and supports_inc
        benchmark_name = path.name

        # Load adapter to get build configuration
        adapter = _load_benchmark_adapter(path, source_mode)
        if not adapter:
            logger.warning(f"Failed to load adapter for {benchmark_name}, skipping")
            continue

        # Determine mode and commit
        ref_commit = adapter.get_ref_commit()
        base_commit = adapter.get_base_commit()

        if ref_commit:
            # Delta mode: use ref_commit
            mode = BenchmarkMode.DELTA
            commit = ref_commit
        elif base_commit:
            # Full mode: use base_commit
            mode = BenchmarkMode.FULL
            commit = base_commit
        else:
            logger.warning(f"No commit found for {benchmark_name}, skipping")
            continue

        main_repo = adapter.main_repo
        language = adapter.lang
        repo_name = adapter.repo_name
        sanitizer = adapter.get_required_sanitizer()

        # Collect all patches for allpatched variant from discovery
        all_patches: list[Path] = []
        harnesses = discover_harness_names(path)
        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                patches = discover_patch_paths(path, harness, cpv_id)
                for _, patch_path in patches:
                    if patch_path not in all_patches:
                        all_patches.append(patch_path)

        # Track build job IDs for this benchmark
        build_job_ids: list[str] = []

        # Create BuildSingleVariantJob for vulnerable variant
        is_delta = mode == BenchmarkMode.DELTA
        vulnerable_variant_type = (
            VariantType.DELTA_REF if is_delta else VariantType.FULL_BASE
        )
        vulnerable_job = BuildSingleVariantJob(
            benchmark_path=path,
            benchmark_name=benchmark_name,
            variant_type=vulnerable_variant_type,
            commit=commit,
            main_repo=main_repo,
            mode=mode,
            language=language,
            use_inc_build=effective_inc,
            force_rebuild=force_rebuild,
            source_mode=source_mode,
            repo_name=repo_name,
            sanitizer=sanitizer,
        )
        all_jobs.append(vulnerable_job)
        build_job_ids.append(vulnerable_job.job_id)

        # Create BuildSingleVariantJob for allpatched variant
        allpatched_job = BuildSingleVariantJob(
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
            source_mode=source_mode,
            repo_name=repo_name,
            sanitizer=sanitizer,
        )
        all_jobs.append(allpatched_job)
        build_job_ids.append(allpatched_job.job_id)

        # Coverage job (only if --inc-coverage)
        coverage_job_id = ""
        if inc_coverage:
            coverage_build_job = BuildSingleVariantJob(
                benchmark_path=path,
                benchmark_name=benchmark_name,
                variant_type=VariantType.COVERAGE,
                commit=commit,
                main_repo=main_repo,
                mode=mode,
                language=language,
                use_inc_build=False,  # Coverage doesn't support inc-build
                force_rebuild=force_rebuild,
                source_mode=source_mode,
                sanitizer="coverage",
                repo_name=repo_name,
            )
            all_jobs.append(coverage_build_job)
            coverage_job_id = coverage_build_job.job_id

        harnesses = discover_harness_names(path)
        cpv_ids: list[str] = []
        patch_keys: list[tuple[str, str]] = []

        for harness in harnesses:
            for cpv_id in discover_cpv_ids(path, harness):
                if cpv_id in cpv_ids:
                    continue
                cpv_ids.append(cpv_id)

                # Extract cpv_num from cpv_id (e.g., "cpv_0" -> 0)
                cpv_num = int(cpv_id.split("_")[1])

                # Get patches for this specific CPV
                cpv_specific_patches = [
                    patch_path
                    for _, patch_path in discover_patch_paths(path, harness, cpv_id)
                ]

                # CPV variant patches = all patches except this CPV's patches
                # This is used to test if the POV triggers this specific vulnerability
                cpv_variant_patches = [
                    p for p in all_patches if p not in cpv_specific_patches
                ]

                # Create BuildSingleVariantJob for this CPV variant
                cpv_build_job = BuildSingleVariantJob(
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
                    source_mode=source_mode,
                    repo_name=repo_name,
                    sanitizer=sanitizer,
                )
                all_jobs.append(cpv_build_job)

                # Build job IDs for this CPV: vulnerable + allpatched + this CPV variant
                cpv_build_job_ids = build_job_ids.copy()
                cpv_build_job_ids.append(cpv_build_job.job_id)

                # POV verify jobs - depend on vulnerable, allpatched, and CPV variant builds
                pov_paths = discover_pov_paths(path, harness, cpv_id)
                # Apply max_povs_per_cpv limit (paths already sorted by pov number)
                if max_povs_per_cpv and len(pov_paths) > max_povs_per_cpv:
                    pov_paths = pov_paths[:max_povs_per_cpv]
                if pov_paths:
                    pov_job = VerifyCpvPovJob(
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        harness=harness,
                        benchmark_path=path,
                        pov_paths=pov_paths,
                        build_job_ids=cpv_build_job_ids,
                        source_mode=source_mode,
                    )
                    all_jobs.append(pov_job)

                # Patch build + test jobs (FULL mode)
                # BuildPatchVariantJob depends on vulnerable build for adapter
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
                        build_job_id=vulnerable_job.job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(build_patch_job)

                    test_full_job = PatchVariantTestJob(
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
                        test_rts_job = PatchVariantTestJob(
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

        # Coverage verification job (only if --inc-coverage)
        if inc_coverage and coverage_job_id:
            harness = harnesses[0] if harnesses else ""
            coverage_verify_job = FlatCollectCoverageJob(
                benchmark_path=path,
                benchmark_name=benchmark_name,
                harness=harness,
                build_job_id=coverage_job_id,
                source_mode=source_mode,
            )
            all_jobs.append(coverage_verify_job)

        benchmark_metadata.append(
            (path, supports_inc, rts_mode, cpv_ids, patch_keys, build_job_ids)
        )

    return all_jobs, benchmark_metadata


def _log_dag_summary(all_jobs: list[Job]) -> None:
    """Log DAG job counts by type."""
    build_single_count = sum(
        1 for j in all_jobs if isinstance(j, BuildSingleVariantJob)
    )
    build_legacy_count = sum(1 for j in all_jobs if isinstance(j, BuildVariantsJob))
    pov_jobs = [j for j in all_jobs if isinstance(j, VerifyCpvPovJob)]
    pov_blob_count = sum(len(j.pov_paths) for j in pov_jobs)
    patch_build_count = sum(1 for j in all_jobs if isinstance(j, BuildPatchVariantJob))
    patch_test_count = sum(1 for j in all_jobs if isinstance(j, PatchVariantTestJob))
    coverage_count = sum(1 for j in all_jobs if isinstance(j, FlatCollectCoverageJob))

    # Report build-single if used, otherwise legacy build-variants
    build_info = (
        f"{build_single_count} build-single"
        if build_single_count > 0
        else f"{build_legacy_count} build"
    )

    logger.info(
        f"DAG: {len(all_jobs)} jobs — "
        f"{build_info}, {len(pov_jobs)} pov-verify ({pov_blob_count} blobs), "
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
    build_job_ids: list[str],
    *,
    inc_coverage: bool = False,
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

    if inc_coverage:
        coverage_result = aggregate_coverage_result(dag_results, benchmark_name)
    else:
        coverage_result = CheckResult.skip("Use --inc-coverage to enable")

    # Collect build time and storage from BuildSingleVariantJob results
    shared_build_time = 0.0
    storage_bytes = 0
    for job_id in build_job_ids:
        build_result = dag_results.get(job_id)
        if build_result:
            shared_build_time += build_result.elapsed_seconds
            if build_result.job_result:
                storage_bytes = max(
                    storage_bytes,
                    build_result.job_result.details.get("storage_bytes", 0),
                )

    # Fallback to legacy BuildVariantsJob if no build_job_ids
    if not build_job_ids:
        build_job_id = f"build-variants:{benchmark_name}"
        build_result = dag_results.get(build_job_id)
        shared_build_time = build_result.elapsed_seconds if build_result else 0.0
        if build_result and build_result.job_result:
            storage_bytes = build_result.job_result.details.get("storage_bytes", 0)

    return BenchmarkValidationResult(
        benchmark=benchmark_name,
        benchmark_path=path,
        format_check=fmt_result,
        pov_check=pov_result,
        patch_check=patch_result,
        patch_rts_check=rts_result,
        coverage_check=coverage_result,
        shared_build_time=shared_build_time,
        storage_bytes=storage_bytes,
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
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)
    inc_coverage = getattr(args, "inc_coverage", False)
    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
        inc_coverage=inc_coverage,
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
    for (
        path,
        supports_inc,
        rts_mode,
        cpv_ids,
        patch_keys,
        build_job_ids,
    ) in benchmark_metadata:
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
                build_job_ids,
                inc_coverage=inc_coverage,
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
