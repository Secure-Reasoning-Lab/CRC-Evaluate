"""CLI command for coverage collection.

This module provides the `crsbench coverage` CLI command for collecting
code coverage from corpus files against benchmark projects.

Usage:
    crsbench coverage <benchmark_path> --corpus-dir <dir> [options]
    crsbench coverage --experiment-config experiment.yaml [options]
    crsbench coverage --experiment-dir experiment-output/ [options]
    crsbench coverage --seed-dir <dir> --benchmark <name> --harness <name> \
        --output-dir <dir> [options]

Examples:
    # Collect coverage for a benchmark
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/

    # Collect coverage with specific harness
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --harness fuzz_parse

    # Output results to JSON file
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --output report.json

  # Parallel experiment coverage across harness pairs
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ \
      --jobs 4 --cores-per-job 8

Note:
    Coverage collection is currently experimental.
"""

import argparse
import json
import os
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional

import yaml

from crsbench.evaluation.coverage.engine import CoverageEngine
from crsbench.evaluation.coverage.models import CoverageSummary, CoverageTimelineReport
from crsbench.evaluation.coverage.reporting import (
    write_timeline_csv,
    write_timeline_json,
    write_timeline_png,
)
from crsbench.evaluation.coverage.timeline import (
    aggregate_line_coverage_buckets,
    load_trial_context,
    normalize_seed_inputs,
)
from crsbench.evaluation.trial_paths import (
    experiment_dir_from_config_dict,
    resolve_benchmark_path,
    resolve_benchmarks_root,
)
from crsbench.utils.cpu_pool import CPUPool, format_cpuset
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

logger = get_logger(__name__)


def _resolve_jobs(args: argparse.Namespace) -> int:
    jobs = getattr(args, "jobs", None)
    legacy_jobs = getattr(args, "build_workers", None)
    if jobs is not None and legacy_jobs is not None and jobs != legacy_jobs:
        raise ValueError("--jobs conflicts with legacy --build-workers")
    resolved = int(jobs if jobs is not None else legacy_jobs or 1)
    if resolved < 1:
        raise ValueError("--jobs must be >= 1")
    return resolved


def _resolve_cores_per_job(args: argparse.Namespace) -> int:
    cores_per_job = getattr(args, "cores_per_job", None)
    legacy_cores = getattr(args, "verify_workers", None)
    if (
        cores_per_job is not None
        and legacy_cores is not None
        and cores_per_job != legacy_cores
    ):
        raise ValueError("--cores-per-job conflicts with legacy --verify-workers")
    resolved = int(cores_per_job if cores_per_job is not None else legacy_cores or 1)
    if resolved < 1:
        raise ValueError("--cores-per-job must be >= 1")
    return resolved


def add_coverage_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the coverage subcommand to argparse.

    Args:
        subparsers: Subparsers action from main argument parser
    """
    parser = subparsers.add_parser(
        "coverage",
        help="Collect code coverage from corpus files",
        description=(
            "Collect code coverage from corpus files against a benchmark project. "
            "Builds a coverage-instrumented variant ({project}-coverage) and runs "
            "corpus files against it to measure code coverage. "
            "This command is currently experimental."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Collect direct coverage summary for a benchmark corpus
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/

  # Analyze seed coverage over time from experiment outputs
  crsbench coverage --experiment-config ./experiment.yaml

  # Analyze seed coverage over time from an experiment output directory
  crsbench coverage --experiment-dir ./experiment-output/

  # Analyze a direct seed directory
  crsbench coverage --seed-dir ./seeds --benchmark sanity-mock-c-delta-01 \
      --harness fuzz_parse_buffer_section --output-dir ./coverage-out

  # Force rebuild of coverage variant
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --force-rebuild

  # Output results as JSON
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --output report.json
        """,
    )

    # Required arguments
    parser.add_argument(
        "benchmark_path",
        type=Path,
        nargs="?",
        help="Path to the benchmark project directory (legacy direct mode)",
    )

    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help="Directory containing corpus files to measure coverage (legacy mode)",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=None,
        help="Directory containing seed files to analyze",
    )
    parser.add_argument(
        "--experiment-config",
        type=Path,
        default=None,
        help="Experiment config YAML to analyze all matching trial outputs",
    )
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=None,
        help="Experiment output directory to analyze all matching trial outputs",
    )
    parser.add_argument(
        "--benchmarks",
        type=Path,
        default=None,
        help="Benchmarks root for direct --seed-dir mode (default: ./benchmarks)",
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default=None,
        help="Benchmark name for direct --seed-dir mode",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for direct --seed-dir timeline artifacts",
    )
    parser.add_argument(
        "--bucket-size-seconds",
        type=int,
        default=1,
        help="Bucket size in seconds for timeline aggregation (default: 1)",
    )

    # Optional arguments
    parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Specific harness name to test (default: first available)",
    )
    parser.add_argument(
        "--oss-fuzz-path",
        type=Path,
        default=None,
        help="Path to oss-fuzz directory (default: managed third_party/oss-fuzz)",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild of coverage variant",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path for results (legacy direct mode)",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="json",
        choices=["json", "yaml", "text"],
        help="Output format (default: json, legacy direct mode only)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help=(
            "Parallel coverage jobs. In experiment modes, this is the number "
            "of benchmark-harness jobs; legacy direct mode still passes this "
            "through as the build worker count."
        ),
    )
    parser.add_argument(
        "--cores-per-job",
        type=int,
        default=None,
        help=(
            "Warm coverage containers per benchmark-harness job. Seeds are "
            "sharded across this many one-core containers."
        ),
    )
    parser.add_argument(
        "--build-workers",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--source",
        type=str,
        default="pkgs",
        choices=["pkgs", "main_repo"],
        help="Source for benchmark code: 'pkgs' uses bundled tarballs (default), "
        "'main_repo' clones from repository",
    )

    parser.set_defaults(func=run_coverage)


def run_coverage(args: argparse.Namespace) -> int:
    """Execute the coverage command.

    Uses CoverageEngine for coverage collection and timeline analysis.
    In experiment mode, ``jobs`` controls parallel ``(benchmark, harness)``
    jobs and ``cores_per_job`` controls the number of warm one-core coverage
    containers per job.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Configure logging
    from crsbench.utils.logger import configure_logger

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=log_level)

    direct_seed_mode = (
        args.seed_dir is not None
        and args.benchmark_path is None
        and args.corpus_dir is None
    )
    experiment_timeline_mode = (
        args.experiment_config is not None or args.experiment_dir is not None
    )

    if args.bucket_size_seconds <= 0:
        logger.error("--bucket-size-seconds must be a positive integer")
        return 1
    try:
        jobs = _resolve_jobs(args)
        cores_per_job = _resolve_cores_per_job(args)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    if args.experiment_config is not None and args.experiment_dir is not None:
        logger.error("--experiment-config cannot be combined with --experiment-dir")
        return 1

    invalid_experiment_args = (
        args.benchmark_path is not None
        or args.corpus_dir is not None
        or args.seed_dir is not None
        or args.benchmark is not None
        or args.harness is not None
        or args.output_dir is not None
    )
    if args.experiment_config is not None and args.benchmarks is not None:
        invalid_experiment_args = True

    if experiment_timeline_mode and invalid_experiment_args:
        logger.error(
            "--experiment-config/--experiment-dir cannot be combined with benchmark_path, "
            "--corpus-dir, --seed-dir, --benchmark, --harness, or --output-dir. "
            "--benchmarks is only supported with --experiment-dir."
        )
        return 1

    if experiment_timeline_mode and (args.output is not None or args.format != "json"):
        logger.error(
            "Experiment timeline mode does not support --output or non-default "
            "--format; it always writes JSON, CSV, and PNG files to the "
            "per-trial coverage directory"
        )
        return 1

    if direct_seed_mode and (
        args.benchmark_path is not None or args.corpus_dir is not None
    ):
        logger.error(
            "Direct --seed-dir timeline mode cannot be combined with benchmark_path "
            "or --corpus-dir"
        )
        return 1

    if direct_seed_mode and (args.output is not None or args.format != "json"):
        logger.error(
            "Direct --seed-dir timeline mode does not support --output or "
            "non-default --format; it always writes JSON, CSV, and PNG files "
            "to --output-dir"
        )
        return 1

    # Validate benchmark path
    if experiment_timeline_mode:
        return _run_experiment_timeline(args)

    if direct_seed_mode:
        return _run_direct_seed_timeline(args)

    corpus_dir = args.corpus_dir or args.seed_dir
    if args.benchmark_path is None:
        logger.error(
            "Legacy coverage mode requires benchmark_path and --corpus-dir/--seed-dir, "
            "or use --experiment-config/--experiment-dir, or use direct --seed-dir with "
            "--benchmark/--harness/--output-dir."
        )
        return 1
    if not args.benchmark_path.exists():
        logger.error(f"Benchmark path not found: {args.benchmark_path}")
        return 1
    if corpus_dir is None or not corpus_dir.exists():
        logger.error(f"Corpus directory not found: {corpus_dir}")
        return 1

    corpus_files = [f for f in corpus_dir.iterdir() if f.is_file()]
    if not corpus_files:
        logger.error(f"Corpus directory is empty: {corpus_dir}")
        return 1

    # Determine oss-fuzz path
    try:
        oss_fuzz_path = args.oss_fuzz_path or Path(ensure_oss_fuzz_root())
    except Exception as e:
        logger.error(f"Failed to resolve OSS-Fuzz directory: {e}")
        return 1
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    logger.info(f"Collecting coverage for benchmark: {args.benchmark_path}")
    logger.info(f"Corpus directory: {corpus_dir} ({len(corpus_files)} files)")
    logger.warning("Coverage collection is experimental.")

    runtime_cpus = _allocate_direct_coverage_cpus(cores_per_job)
    if runtime_cpus is None:
        return 1

    # Create engine and collect coverage
    engine = CoverageEngine(
        oss_fuzz_path=oss_fuzz_path,
        build_workers=jobs,
        runtime_workers=cores_per_job,
        runtime_cpus=runtime_cpus,
        source_mode=args.source,
    )

    try:
        report = engine.collect_coverage(
            benchmark_path=args.benchmark_path,
            corpus_dir=corpus_dir,
            harness_filter=args.harness,
            force_rebuild=args.force_rebuild,
        )

        # Check for empty report (indicates failure)
        if not report.harness_name:
            logger.error("Coverage collection failed - no harness processed")
            return 1

        # Check for missing summary
        if report.final_summary is None:
            logger.error("Coverage collection failed - no coverage data")
            return 1

        # Output results
        output_report(
            report.harness_name, report.final_summary, args.output, args.format
        )

        # Print summary
        print_summary(report.final_summary, report.harness_name)

        return 0

    except Exception as e:
        logger.error(f"Coverage collection failed: {e}", exc_info=True)
        return 1

    finally:
        engine.cleanup()


def _run_experiment_timeline(args: argparse.Namespace) -> int:
    """Analyze seed coverage over time for all trials in an experiment."""
    jobs_requested = _resolve_jobs(args)
    cores_per_job = _resolve_cores_per_job(args)
    if args.experiment_config is not None:
        config_path = args.experiment_config
        if not config_path.exists():
            logger.error(f"Experiment config not found: {config_path}")
            return 1

        config = yaml.safe_load(config_path.read_text())
        if not isinstance(config, dict):
            logger.error(f"Invalid experiment config: {config_path}")
            return 1

        experiment_dir = experiment_dir_from_config_dict(config)
        benchmarks_root = resolve_benchmarks_root(config.get("benchmarks_root"))
    else:
        experiment_dir = args.experiment_dir
        assert experiment_dir is not None
        benchmarks_root = resolve_benchmarks_root(args.benchmarks)

    if not experiment_dir.exists():
        logger.error(f"Experiment directory not found: {experiment_dir}")
        return 1
    try:
        oss_fuzz_path = args.oss_fuzz_path or Path(ensure_oss_fuzz_root())
    except Exception as e:
        logger.error(f"Failed to resolve OSS-Fuzz directory: {e}")
        return 1
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    trial_dirs = sorted(
        trial_dir
        for trial_dir in experiment_dir.rglob("trial-*")
        if trial_dir.is_dir() and (trial_dir / "metadata.json").exists()
    )
    if not trial_dirs:
        logger.error(f"No trial directories found under {experiment_dir}")
        return 1

    trial_jobs = []
    try:
        for trial_dir in trial_dirs:
            try:
                context = load_trial_context(trial_dir)
            except FileNotFoundError:
                logger.debug(f"Skipping trial without seeds: {trial_dir}")
                continue
            trial_jobs.append(
                (
                    trial_dir,
                    context,
                    resolve_benchmark_path(context.benchmark, benchmarks_root),
                )
            )

        if not trial_jobs:
            logger.error(
                f"No analyzable trials with seeds found under {experiment_dir}"
            )
            return 1

        cpu_ids = _available_coverage_cpus()
        if len(cpu_ids) < cores_per_job:
            logger.error(
                "Coverage requires %d CPU(s) per benchmark-harness job, but only "
                "%d CPU(s) are available",
                cores_per_job,
                len(cpu_ids),
            )
            return 1

        max_parallel_jobs = min(
            jobs_requested,
            max(1, len(cpu_ids) // cores_per_job),
            len(trial_jobs),
        )
        if max_parallel_jobs < jobs_requested:
            logger.info(
                "Limiting coverage concurrency to %d job(s) based on %d available "
                "CPU(s) and %d core(s) per job",
                max_parallel_jobs,
                len(cpu_ids),
                cores_per_job,
            )

        analyzed_trials = _run_trial_jobs(
            args=args,
            oss_fuzz_path=oss_fuzz_path,
            trial_jobs=trial_jobs,
            max_parallel_jobs=max_parallel_jobs,
            cores_per_job=cores_per_job,
            cpu_ids=cpu_ids,
        )
        if analyzed_trials == 0:
            logger.error(
                f"No analyzable trials with seeds found under {experiment_dir}"
            )
            return 1
        logger.info(f"Saved coverage results under {experiment_dir}")
        return 0
    except Exception as e:
        logger.error(f"Coverage timeline failed: {e}", exc_info=True)
        return 1


def _run_direct_seed_timeline(args: argparse.Namespace) -> int:
    """Analyze seed coverage over time for an explicit seed directory."""
    jobs = _resolve_jobs(args)
    cores_per_job = _resolve_cores_per_job(args)
    if args.seed_dir is None or not args.seed_dir.exists():
        logger.error(f"Seed directory not found: {args.seed_dir}")
        return 1
    if not args.benchmark:
        logger.error("--benchmark is required with --seed-dir")
        return 1
    if not args.harness:
        logger.error("--harness is required with --seed-dir")
        return 1
    if args.output_dir is None:
        logger.error("--output-dir is required with --seed-dir")
        return 1

    benchmark_path = resolve_benchmark_path(args.benchmark, args.benchmarks)
    if not benchmark_path.exists():
        logger.error(f"Benchmark path not found: {benchmark_path}")
        return 1

    try:
        oss_fuzz_path = args.oss_fuzz_path or Path(ensure_oss_fuzz_root())
    except Exception as e:
        logger.error(f"Failed to resolve OSS-Fuzz directory: {e}")
        return 1
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    runtime_cpus = _allocate_direct_coverage_cpus(cores_per_job)
    if runtime_cpus is None:
        return 1

    engine = CoverageEngine(
        oss_fuzz_path=oss_fuzz_path,
        build_workers=jobs,
        runtime_workers=cores_per_job,
        runtime_cpus=runtime_cpus,
        source_mode=args.source,
    )
    try:
        report = _build_timeline_report(
            engine=engine,
            benchmark_path=benchmark_path,
            harness_name=args.harness,
            seed_dir=args.seed_dir,
            crs_run_start_time=None,
            pov_markers=[],
            bucket_size_seconds=args.bucket_size_seconds,
            force_rebuild=args.force_rebuild,
            output_dir=args.output_dir,
        )
        _write_timeline_outputs(report, args.output_dir)
        logger.info(f"Wrote coverage timeline to {args.output_dir}")
        logger.info(f"Saved coverage results at {args.output_dir}")
        return 0
    except Exception as e:
        logger.error(f"Coverage timeline failed: {e}", exc_info=True)
        return 1
    finally:
        engine.cleanup()


def _build_timeline_report(
    *,
    engine: CoverageEngine,
    benchmark_path: Path,
    harness_name: str,
    seed_dir: Path,
    crs_run_start_time: Optional[float],
    pov_markers: list,
    bucket_size_seconds: int,
    force_rebuild: bool,
    output_dir: Path,
) -> CoverageTimelineReport:
    """Build a coverage-over-time report for one seed set."""
    normalized_inputs = normalize_seed_inputs(seed_dir, base_time=crs_run_start_time)
    if not normalized_inputs:
        msg = f"No seeds found to analyze in {seed_dir}"
        raise ValueError(msg)
    timed_inputs, summary = engine.collect_timed_line_coverage(
        benchmark_path=benchmark_path,
        timed_inputs=normalized_inputs,
        harness_filter=harness_name,
        force_rebuild=force_rebuild,
        output_dir=output_dir,
    )
    buckets = aggregate_line_coverage_buckets(
        timed_inputs,
        lines_total=summary.lines_total,
        bucket_size_seconds=bucket_size_seconds,
    )
    return CoverageTimelineReport(
        benchmark=benchmark_path.name,
        harness=harness_name,
        bucket_size_seconds=bucket_size_seconds,
        time_origin=(
            "crs_run_start_time"
            if crs_run_start_time is not None
            else "first_seed_mtime"
        ),
        seeds=timed_inputs,
        pov_markers=pov_markers,
        buckets=buckets,
        final_summary=summary,
    )


def _available_coverage_cpus() -> list[int]:
    """Return CPU IDs available to pin local coverage containers."""
    try:
        return sorted(os.sched_getaffinity(0))
    except AttributeError:
        return list(range(os.cpu_count() or 1))


def _allocate_direct_coverage_cpus(cores_per_job: int) -> Optional[list[int]]:
    cpu_ids = _available_coverage_cpus()
    if len(cpu_ids) < cores_per_job:
        logger.error(
            "Coverage requires %d CPU(s) for this benchmark-harness run, but only "
            "%d CPU(s) are available",
            cores_per_job,
            len(cpu_ids),
        )
        return None
    return cpu_ids[:cores_per_job]


def _run_single_trial_job(
    *,
    args: argparse.Namespace,
    oss_fuzz_path: Path,
    trial_dir: Path,
    context,
    benchmark_path: Path,
    allocated_cpus: list[int],
    cores_per_job: int,
) -> Path:
    engine = CoverageEngine(
        oss_fuzz_path=oss_fuzz_path,
        build_workers=1,
        runtime_workers=cores_per_job,
        runtime_cpus=allocated_cpus,
        source_mode=args.source,
    )
    try:
        report = _build_timeline_report(
            engine=engine,
            benchmark_path=benchmark_path,
            harness_name=context.harness,
            seed_dir=context.seed_dir,
            crs_run_start_time=context.crs_run_start_time,
            pov_markers=context.pov_markers,
            bucket_size_seconds=args.bucket_size_seconds,
            force_rebuild=args.force_rebuild,
            output_dir=trial_dir / "coverage",
        )
        output_dir = trial_dir / "coverage"
        _write_timeline_outputs(report, output_dir)
        logger.info(
            "Wrote coverage timeline to %s using CPUs %s",
            output_dir,
            format_cpuset(allocated_cpus),
        )
        return trial_dir
    finally:
        engine.cleanup()


def _run_trial_jobs(
    *,
    args: argparse.Namespace,
    oss_fuzz_path: Path,
    trial_jobs: list[tuple[Path, object, Path]],
    max_parallel_jobs: int,
    cores_per_job: int,
    cpu_ids: list[int],
) -> int:
    cpu_pool = CPUPool(cores=format_cpuset(cpu_ids))
    pending_jobs = list(trial_jobs)
    running: dict[Future[Path], list[int]] = {}
    analyzed_trials = 0
    first_error: Optional[BaseException] = None

    def _submit_pending(executor: ThreadPoolExecutor) -> None:
        while pending_jobs and len(running) < max_parallel_jobs:
            allocated_cpus = cpu_pool.allocate(cores_per_job)
            if allocated_cpus is None:
                return
            trial_dir, context, benchmark_path = pending_jobs.pop(0)
            future = executor.submit(
                _run_single_trial_job,
                args=args,
                oss_fuzz_path=oss_fuzz_path,
                trial_dir=trial_dir,
                context=context,
                benchmark_path=benchmark_path,
                allocated_cpus=allocated_cpus,
                cores_per_job=cores_per_job,
            )
            running[future] = allocated_cpus

    with ThreadPoolExecutor(max_workers=max_parallel_jobs) as executor:
        _submit_pending(executor)
        while running:
            done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                allocated_cpus = running.pop(future)
                cpu_pool.release(allocated_cpus)
                try:
                    future.result()
                    analyzed_trials += 1
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                break
            _submit_pending(executor)

    if first_error is not None:
        raise first_error
    return analyzed_trials


def _write_timeline_outputs(report: CoverageTimelineReport, output_dir: Path) -> None:
    """Write JSON, CSV, and PNG timeline outputs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    write_timeline_json(report, output_dir / "coverage_timeline.json")
    write_timeline_csv(report, output_dir / "coverage_timeline.csv")
    write_timeline_png(report, output_dir / "coverage_timeline.png")


def output_report(
    harness_name: str,
    summary: CoverageSummary,
    output_path: Optional[Path],
    output_format: str,
) -> None:
    """Output coverage report.

    Args:
        harness_name: Name of the harness
        summary: CoverageSummary with coverage statistics
        output_path: Optional output file path
        output_format: Output format (json, yaml, text)
    """
    result = {
        "harness": harness_name,
        "summary": summary.model_dump(),
    }

    if output_format == "json":
        output = json.dumps(result, indent=2)
    elif output_format == "yaml":
        output = yaml.dump(result, default_flow_style=False)
    else:  # text
        output = (
            f"Harness: {harness_name}\n"
            f"Lines Covered: {summary.format_lines()}\n"
            f"Functions Covered: {summary.format_functions()}\n"
            f"Corpus Files: {summary.corpus_total} "
            f"(contributing: {summary.corpus_contributing}, unique: {summary.corpus_unique})"
        )

    if output_path:
        output_path.write_text(output)
        logger.info(f"Results written to: {output_path}")
    else:
        logger.info(output)


def print_summary(summary: CoverageSummary, harness_name: str) -> None:
    """Print a summary of coverage results.

    Args:
        summary: Coverage summary
        harness_name: Name of the harness
    """
    logger.info("=" * 50)
    logger.info("COVERAGE SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Harness: {harness_name}")
    logger.info(f"Lines: {summary.format_lines()}")
    logger.info(f"Functions: {summary.format_functions()}")
    logger.info(
        f"Corpus: {summary.corpus_total} total, "
        f"{summary.corpus_contributing} contributing, "
        f"{summary.corpus_unique} unique"
    )
    logger.info("=" * 50)


def main() -> None:
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="CRSBench Coverage Collection Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    add_coverage_subparser(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if hasattr(args, "func"):
        sys.exit(args.func(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
