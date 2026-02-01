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
    aggregate_patch_build_results,
    aggregate_patch_pov_results,
    aggregate_patch_results,
    aggregate_patch_unittest_results,
    aggregate_patch_var_results,
    aggregate_pov_build_results,
    aggregate_pov_pov_results,
    aggregate_pov_results,
    aggregate_pov_var_results,
)
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildSingleVariantJob,
    FlatCollectCoverageJob,
    PatchPovTestJob,
    PatchUnitTestJob,
    PatchVariantTestJob,
    PatchVarTestJob,
    VerifyCpvPovJob,
    VerifyCpvVarJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.benchmark_ci.jobs.base import Job
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
    return engine.load_adapter(path)


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
    parser.add_argument(
        "--distributed",
        action="store_true",
        default=False,
        help="Distribute build phase to Redis/RQ workers (verify/patch runs locally)",
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

        # Get all unique sanitizers used by CPVs (supports mixed sanitizers)
        required_sanitizers = adapter.get_all_cpv_sanitizers()
        logger.debug(f"{benchmark_name} requires sanitizers: {required_sanitizers}")

        # Collect all patches for allpatched variant using infrastructure
        # This respects patch_superset relationships (skips subset patches)
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(Path("oss-fuzz"))
        all_patches = infra.get_all_patches(path)

        # Build shared variants (deltaref, allpatched) for each sanitizer
        # Track build job IDs by sanitizer: {sanitizer: [deltaref_id, allpatched_id]}
        build_jobs_by_sanitizer: dict[str, list[str]] = {}
        is_delta = mode == BenchmarkMode.DELTA
        vulnerable_variant_type = (
            VariantType.DELTA_REF if is_delta else VariantType.FULL_BASE
        )

        for sanitizer in required_sanitizers:
            # Create vulnerable variant for this sanitizer
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
                sanitizer=sanitizer,
                repo_name=repo_name,
            )
            all_jobs.append(vulnerable_job)

            # Create allpatched variant for this sanitizer
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
                sanitizer=sanitizer,
                repo_name=repo_name,
            )
            all_jobs.append(allpatched_job)

            # Track IDs for this sanitizer
            build_jobs_by_sanitizer[sanitizer] = [
                vulnerable_job.job_id,
                allpatched_job.job_id,
            ]

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

                # Get sanitizer for this specific CPV (supports mixed sanitizers per harness)
                cpv_sanitizer = adapter.get_cpv_sanitizer(harness, cpv_id)

                # CPV variant patches = all patches except this CPV's patches
                # This uses get_patches_except() which respects patch_superset relationships
                cpv_variant_patches = infra.get_patches_except(path, cpv_num)

                # Create BuildSingleVariantJob for this CPV variant
                # Use CPV-specific sanitizer (supports mixed sanitizers within harness)
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
                    sanitizer=cpv_sanitizer,
                    repo_name=repo_name,
                )
                all_jobs.append(cpv_build_job)

                # Build job IDs for this CPV: vulnerable + allpatched + CPV variant
                # Use only the shared variants that match this CPV's sanitizer
                cpv_build_job_ids = build_jobs_by_sanitizer[cpv_sanitizer].copy()
                cpv_build_job_ids.append(cpv_build_job.job_id)

                # Discover POV paths and split into pov_0 and variants
                pov_paths = discover_pov_paths(path, harness, cpv_id)
                # Apply max_povs_per_cpv limit (paths already sorted by pov number)
                if max_povs_per_cpv and len(pov_paths) > max_povs_per_cpv:
                    pov_paths = pov_paths[:max_povs_per_cpv]

                # Split pov_0 (ground truth) from variants (pov_1+)
                pov_0_path: Path | None = None
                var_pov_paths: list[Path] = []
                for p in pov_paths:
                    if p.stem in ("pov_0", "pov"):
                        pov_0_path = p
                    else:
                        var_pov_paths.append(p)

                # V:POV job - tests only pov_0 (ground truth)
                if pov_0_path:
                    pov_job = VerifyCpvPovJob(
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        harness=harness,
                        benchmark_path=path,
                        pov_path=pov_0_path,
                        build_job_ids=cpv_build_job_ids,
                        source_mode=source_mode,
                    )
                    all_jobs.append(pov_job)

                # V:VAR job - tests only variants (pov_1+), runs parallel to V:POV
                if var_pov_paths:
                    var_job = VerifyCpvVarJob(
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        harness=harness,
                        benchmark_path=path,
                        pov_paths=var_pov_paths,
                        build_job_ids=cpv_build_job_ids,
                        source_mode=source_mode,
                    )
                    all_jobs.append(var_job)

                # Patch build + test jobs
                # New structure: build -> (P:POV, P:VAR, P:UT, P:RTS) all in parallel
                # BuildPatchVariantJob depends on vulnerable build for adapter
                # Use sanitizer-specific vulnerable job (first in the list)
                cpv_vulnerable_job_id = build_jobs_by_sanitizer[cpv_sanitizer][0]

                patches = discover_patch_paths(path, harness, cpv_id)
                for patch_id, patch_path in patches:
                    patch_keys.append((cpv_id, patch_id))

                    # Step 1: Build patch
                    build_patch_job = BuildPatchVariantJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        patch_path=patch_path,
                        harness=harness,  # Pass harness for per-CPV sanitizer
                        use_inc_build=effective_inc,
                        force_rebuild=force_rebuild,
                        build_job_id=cpv_vulnerable_job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(build_patch_job)

                    # Step 2a: P:POV - tests only pov_0 (depends on build)
                    if pov_0_path:
                        pov_test_job = PatchPovTestJob(
                            benchmark_path=path,
                            benchmark_name=benchmark_name,
                            cpv_id=cpv_id,
                            patch_id=patch_id,
                            harness=harness,
                            pov_path=pov_0_path,
                            build_patch_job_id=build_patch_job.job_id,
                            source_mode=source_mode,
                        )
                        all_jobs.append(pov_test_job)

                    # Step 2b: P:VAR - tests only variants (depends on build, parallel to P:POV)
                    if var_pov_paths:
                        var_test_job = PatchVarTestJob(
                            benchmark_path=path,
                            benchmark_name=benchmark_name,
                            cpv_id=cpv_id,
                            patch_id=patch_id,
                            harness=harness,
                            pov_paths=var_pov_paths,
                            build_patch_job_id=build_patch_job.job_id,
                            source_mode=source_mode,
                        )
                        all_jobs.append(var_test_job)

                    # Step 2c: P:UT FULL - unit tests (depends on build, parallel to POV/VAR)
                    unittest_full_job = PatchUnitTestJob(
                        benchmark_path=path,
                        benchmark_name=benchmark_name,
                        cpv_id=cpv_id,
                        patch_id=patch_id,
                        harness=harness,
                        test_mode="FULL",
                        build_patch_job_id=build_patch_job.job_id,
                        source_mode=source_mode,
                    )
                    all_jobs.append(unittest_full_job)

                    # Step 2d: P:RTS - RTS unit tests (if available, parallel to others)
                    if rts_mode:
                        unittest_rts_job = PatchUnitTestJob(
                            benchmark_path=path,
                            benchmark_name=benchmark_name,
                            cpv_id=cpv_id,
                            patch_id=patch_id,
                            harness=harness,
                            test_mode="RTS",
                            build_patch_job_id=build_patch_job.job_id,
                            source_mode=source_mode,
                        )
                        all_jobs.append(unittest_rts_job)

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

        # Flatten all shared build job IDs across sanitizers for aggregation
        all_shared_build_job_ids = []
        for job_ids in build_jobs_by_sanitizer.values():
            all_shared_build_job_ids.extend(job_ids)

        benchmark_metadata.append(
            (
                path,
                supports_inc,
                rts_mode,
                cpv_ids,
                patch_keys,
                all_shared_build_job_ids,
            )
        )

    return all_jobs, benchmark_metadata


def _log_dag_summary(all_jobs: list[Job]) -> None:
    """Log DAG job counts by type."""
    build_single_count = sum(
        1 for j in all_jobs if isinstance(j, BuildSingleVariantJob)
    )
    # Count new split verify jobs (V:POV and V:VAR)
    pov_job_count = sum(1 for j in all_jobs if isinstance(j, VerifyCpvPovJob))
    var_job_count = sum(1 for j in all_jobs if isinstance(j, VerifyCpvVarJob))
    var_pov_count = sum(
        len(j.pov_paths) for j in all_jobs if isinstance(j, VerifyCpvVarJob)
    )

    patch_build_count = sum(1 for j in all_jobs if isinstance(j, BuildPatchVariantJob))
    # Count new split patch jobs (P:POV, P:VAR, P:UT)
    patch_pov_count = sum(1 for j in all_jobs if isinstance(j, PatchPovTestJob))
    patch_var_count = sum(1 for j in all_jobs if isinstance(j, PatchVarTestJob))
    patch_unittest_count = sum(1 for j in all_jobs if isinstance(j, PatchUnitTestJob))
    # Legacy combined job count
    patch_test_count = sum(1 for j in all_jobs if isinstance(j, PatchVariantTestJob))
    coverage_count = sum(1 for j in all_jobs if isinstance(j, FlatCollectCoverageJob))

    build_info = f"{build_single_count} build-single"

    # Report new split verify structure
    verify_info = (
        f"{pov_job_count} V:POV, {var_job_count} V:VAR ({var_pov_count} blobs)"
    )

    # Report new split patch structure if used, otherwise legacy combined
    if patch_pov_count > 0:
        patch_info = (
            f"{patch_build_count} P:Bld, {patch_pov_count} P:POV, "
            f"{patch_var_count} P:VAR, {patch_unittest_count} P:UT"
        )
    else:
        patch_info = f"{patch_build_count} patch-build, {patch_test_count} patch-test"

    logger.info(
        f"DAG: {len(all_jobs)} jobs — "
        f"{build_info}, {verify_info}, "
        f"{patch_info}, {coverage_count} coverage"
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

    # Split POV results for detailed CSV output
    pov_build_result = aggregate_pov_build_results(dag_results, benchmark_name)
    pov_pov_result = aggregate_pov_pov_results(dag_results, benchmark_name, cpv_ids)
    pov_var_result = aggregate_pov_var_results(dag_results, benchmark_name, cpv_ids)

    patch_result = aggregate_patch_results(
        dag_results, benchmark_name, patch_keys, test_mode="FULL"
    )

    # Split patch results for detailed CSV output
    patch_build_result = aggregate_patch_build_results(
        dag_results, benchmark_name, patch_keys
    )
    patch_pov_result = aggregate_patch_pov_results(
        dag_results, benchmark_name, patch_keys
    )
    patch_var_result = aggregate_patch_var_results(
        dag_results, benchmark_name, patch_keys
    )
    patch_unittest_result = aggregate_patch_unittest_results(
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

    return BenchmarkValidationResult(
        benchmark=benchmark_name,
        benchmark_path=path,
        format_check=fmt_result,
        pov_check=pov_result,
        pov_build_check=pov_build_result,
        pov_pov_check=pov_pov_result,
        pov_var_check=pov_var_result,
        patch_check=patch_result,
        patch_build_check=patch_build_result,
        patch_pov_check=patch_pov_result,
        patch_var_check=patch_var_result,
        patch_unittest_check=patch_unittest_result,
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

    source_mode = getattr(args, "source", "pkgs")
    build_workers = getattr(args, "build_workers", 4)
    verify_workers = getattr(args, "verify_workers", 4)
    use_inc_build = not getattr(args, "no_inc_build", False)
    force_rebuild = getattr(args, "force_rebuild", True)
    distributed = getattr(args, "distributed", False)
    redis_host = getattr(args, "redis_host", "localhost")

    build_mode = "inc-build" if use_inc_build else "full-build"
    rebuild_mode = "force-rebuild" if force_rebuild else "cached"
    exec_mode = f", distributed (redis={redis_host})" if distributed else ""
    logger.info(
        f"Running all: {len(paths)} benchmark(s), "
        f"build-workers={build_workers}, verify-workers={verify_workers}, "
        f"{build_mode}, {rebuild_mode}{exec_mode}"
    )

    start_dt = datetime.now()

    # Phase 1: Format validation (fast, no DAG needed)
    format_results: dict[str, CheckResult] = {}
    for path in paths:
        fmt_result = validate_format(path, source_mode)
        format_results[path.name] = fmt_result.format_check or CheckResult.make_error(
            "format check not run"
        )

    # Phase 2: Build via VariantPlanner + Redis, verify/test via DAG
    max_povs_per_cpv = getattr(args, "max_povs_per_cpv", None)
    inc_coverage = getattr(args, "inc_coverage", False)

    # Step 2a: Create build jobs via VariantPlanner
    from crsbench.executor.variant_planner import VariantPlanner

    planner = VariantPlanner(oss_fuzz_path=Path("oss-fuzz"), source_mode=source_mode)
    vp_build_jobs = planner.plan_all_builds(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        skip_if_cached=False,
        include_coverage=inc_coverage,
    )

    # Step 2b: Send builds to Redis
    from crsbench.benchmark_ci.cli.commands.build_cmd import _run_distributed_build

    logger.info(
        f"VariantPlanner: {len(vp_build_jobs)} build jobs via Redis, redis={redis_host}"
    )
    build_results = _run_distributed_build(vp_build_jobs, redis_host)

    # Step 2c: Build full DAG (includes build + verify/test jobs)
    all_jobs, benchmark_metadata = _build_dag(
        list(paths),
        use_inc_build=use_inc_build,
        force_rebuild=force_rebuild,
        source_mode=source_mode,
        max_povs_per_cpv=max_povs_per_cpv,
        inc_coverage=inc_coverage,
    )

    # Filter out build jobs (already completed via Redis)
    remaining_jobs = [j for j in all_jobs if not isinstance(j, BuildSingleVariantJob)]
    _log_dag_summary(all_jobs)
    logger.info(
        f"Build phase complete: {len(vp_build_jobs)} builds via Redis, "
        f"{len(remaining_jobs)} verify/patch jobs via Redis"
    )

    # Step 2d: Execute remaining verify/test jobs via Redis
    from crsbench.distributed.ci_jobs import (
        ci_results_to_executor_results,
        enqueue_and_poll_ci_jobs,
    )

    verify_queue_name = f"crsbench_ci_{redis_host}_verify"
    raw_verify_results = enqueue_and_poll_ci_jobs(
        remaining_jobs, redis_host, queue_name=verify_queue_name
    )
    verify_results = ci_results_to_executor_results(raw_verify_results)
    dag_results = {**build_results, **verify_results}

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
