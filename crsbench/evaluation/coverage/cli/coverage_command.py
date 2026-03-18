"""CLI command for Atlantis-backed coverage collection."""

import argparse
import os
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional

import yaml

from crsbench.evaluation.coverage.engine import CoverageEngine
from crsbench.evaluation.coverage.models import CoverageTimelineReport
from crsbench.evaluation.coverage.reporting import (
    write_timeline_csv,
    write_timeline_json,
    write_timeline_png,
)
from crsbench.evaluation.coverage.timeline import (
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

logger = get_logger(__name__)


def _resolve_jobs(args: argparse.Namespace) -> int:
    jobs = getattr(args, "jobs", None)
    resolved = int(jobs if jobs is not None else 1)
    if resolved < 1:
        raise ValueError("--jobs must be >= 1")
    return resolved


def _resolve_cores_per_job(args: argparse.Namespace) -> int:
    cores_per_job = getattr(args, "cores_per_job", None)
    resolved = int(cores_per_job if cores_per_job is not None else 1)
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
        help="Analyze coverage timelines from seeds or experiment outputs",
        description=(
            "Analyze seed coverage against a benchmark project or experiment "
            "output using the Atlantis/libCRS warm-runner backend."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze seed coverage over time from experiment outputs
  crsbench coverage --experiment-config ./experiment.yaml

  # Analyze seed coverage over time from an experiment output directory
  crsbench coverage --experiment-dir ./experiment-filestore/experiment-name

  # Analyze a direct seed directory
  crsbench coverage --seed-dir ./seeds --benchmark sanity-mock-c-delta-01 \
      --harness fuzz_parse_buffer_section --output-dir ./coverage-out
        """,
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
        help=(
            "Output directory for coverage timeline artifacts. Direct --seed-dir "
            "writes here directly. Experiment modes mirror "
            "<experiment-name>/.../trial-N/coverage under this root."
        ),
    )
    parser.add_argument(
        "--experiment-start-time",
        type=float,
        default=None,
        help=(
            "Unix timestamp in seconds used as the direct --seed-dir time origin. "
            "If omitted, direct mode uses the first retained seed mtime."
        ),
    )
    # Optional arguments
    parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Specific harness name to test (default: first available)",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild of coverage variant",
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
            "of benchmark-harness jobs."
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

    direct_seed_mode = args.seed_dir is not None
    experiment_timeline_mode = (
        args.experiment_config is not None or args.experiment_dir is not None
    )

    try:
        _resolve_jobs(args)
        _resolve_cores_per_job(args)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    if args.experiment_config is not None and args.experiment_dir is not None:
        logger.error("--experiment-config cannot be combined with --experiment-dir")
        return 1

    invalid_experiment_args = (
        args.seed_dir is not None
        or args.benchmark is not None
        or args.harness is not None
        or args.experiment_start_time is not None
    )
    if args.experiment_config is not None and args.benchmarks is not None:
        invalid_experiment_args = True

    if experiment_timeline_mode and invalid_experiment_args:
        logger.error(
            "--experiment-config/--experiment-dir cannot be combined with "
            "--seed-dir, --benchmark, --harness, or --experiment-start-time. "
            "--benchmarks is only supported with --experiment-dir."
        )
        return 1

    # Validate benchmark path
    if experiment_timeline_mode:
        return _run_experiment_timeline(args)

    if direct_seed_mode:
        return _run_direct_seed_timeline(args)

    logger.error(
        "Coverage analysis requires --experiment-config, --experiment-dir, or "
        "--seed-dir with --benchmark, --harness, and --output-dir."
    )
    return 1


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
            timing_error = _validate_experiment_timeline_context(context)
            if timing_error is not None:
                logger.error(f"{trial_dir}: {timing_error}")
                return 1
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
                "Coverage requires {} CPU(s) per benchmark-harness job, but only "
                "{} CPU(s) are available",
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
                f"Limiting coverage concurrency to {max_parallel_jobs} job(s) "
                f"based on {len(cpu_ids)} available CPU(s) and "
                f"{cores_per_job} core(s) per job"
            )

        analyzed_trials = _run_trial_jobs(
            args=args,
            trial_jobs=trial_jobs,
            max_parallel_jobs=max_parallel_jobs,
            cores_per_job=cores_per_job,
            cpu_ids=cpu_ids,
            experiment_dir=experiment_dir,
        )
        if analyzed_trials == 0:
            logger.error(
                f"No analyzable trials with seeds found under {experiment_dir}"
            )
            return 1
        logger.info(
            "Saved coverage results under {}",
            _resolve_experiment_output_root(
                experiment_dir=experiment_dir,
                output_base=args.output_dir,
            ),
        )
        return 0
    except Exception as e:
        logger.error("Coverage timeline failed: {}", e, exc_info=True)
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

    runtime_cpus = _allocate_direct_coverage_cpus(cores_per_job)
    if runtime_cpus is None:
        return 1

    engine = CoverageEngine(
        jobs=jobs,
        runtime_workers=cores_per_job,
        runtime_cpus=runtime_cpus,
        source_mode=args.source,
    )
    try:
        time_origin_base = args.experiment_start_time
        time_origin = (
            "experiment_start_time"
            if args.experiment_start_time is not None
            else "first_seed_mtime"
        )
        report = _build_timeline_report(
            engine=engine,
            benchmark_path=benchmark_path,
            harness_name=args.harness,
            seed_dir=args.seed_dir,
            time_origin_base=time_origin_base,
            time_origin=time_origin,
            pov_markers=[],
            timeline_duration_seconds=None,
            force_rebuild=args.force_rebuild,
            output_dir=args.output_dir,
        )
        _write_timeline_outputs(report, args.output_dir)
        logger.info(f"Wrote coverage timeline to {args.output_dir}")
        logger.info(f"Saved coverage results at {args.output_dir}")
        return 0
    except Exception as e:
        logger.error("Coverage timeline failed: {}", e, exc_info=True)
        return 1
    finally:
        engine.cleanup()


def _build_timeline_report(
    *,
    engine: CoverageEngine,
    benchmark_path: Path,
    harness_name: str,
    seed_dir: Path,
    time_origin_base: Optional[float],
    time_origin: str,
    pov_markers: list,
    timeline_duration_seconds: Optional[float],
    force_rebuild: bool,
    output_dir: Path,
) -> CoverageTimelineReport:
    """Build a coverage-over-time report for one seed set."""
    if time_origin == "crs_run_start_time":
        if time_origin_base is None:
            msg = (
                "Experiment-backed coverage requires crs_run_start_time from "
                "povs/pov_store.json"
            )
            raise ValueError(msg)
        if timeline_duration_seconds is None:
            msg = (
                "Experiment-backed coverage requires run_time in trial metadata to "
                "bound the timeline"
            )
            raise ValueError(msg)

    normalized_inputs = normalize_seed_inputs(
        seed_dir,
        base_time=time_origin_base,
        clamp_negative_to_zero=(time_origin == "crs_run_start_time"),
    )
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
    return CoverageTimelineReport(
        benchmark=benchmark_path.name,
        harness=harness_name,
        time_origin=time_origin,
        timeline_duration_seconds=timeline_duration_seconds,
        seeds=timed_inputs,
        pov_markers=pov_markers,
        final_summary=summary,
    )


def _validate_experiment_timeline_context(context: object) -> Optional[str]:
    """Return an error message when experiment timeline metadata is incomplete."""
    crs_run_start_time = getattr(context, "crs_run_start_time", None)
    if crs_run_start_time is None:
        return "missing crs_run_start_time in povs/pov_store.json"
    timeline_duration_seconds = getattr(context, "timeline_duration_seconds", None)
    if timeline_duration_seconds is None:
        return "missing run_time in metadata.json"
    return None


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
            "Coverage requires {} CPU(s) for this benchmark-harness run, but only "
            "{} CPU(s) are available",
            cores_per_job,
            len(cpu_ids),
        )
        return None
    return cpu_ids[:cores_per_job]


def _run_single_trial_job(
    *,
    args: argparse.Namespace,
    trial_dir: Path,
    experiment_dir: Path,
    context,
    benchmark_path: Path,
    allocated_cpus: list[int],
    cores_per_job: int,
) -> Path:
    output_dir = _resolve_trial_coverage_output_dir(
        trial_dir=trial_dir,
        experiment_dir=experiment_dir,
        output_base=args.output_dir,
    )
    engine = CoverageEngine(
        jobs=1,
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
            time_origin_base=context.crs_run_start_time,
            time_origin="crs_run_start_time",
            pov_markers=context.pov_markers,
            timeline_duration_seconds=context.timeline_duration_seconds,
            force_rebuild=args.force_rebuild,
            output_dir=output_dir,
        )
        _write_timeline_outputs(report, output_dir)
        logger.info(
            f"Wrote coverage timeline to {output_dir} using CPUs "
            f"{format_cpuset(allocated_cpus)}"
        )
        return trial_dir
    finally:
        engine.cleanup()


def _run_trial_jobs(
    *,
    args: argparse.Namespace,
    trial_jobs: list[tuple[Path, object, Path]],
    max_parallel_jobs: int,
    cores_per_job: int,
    cpu_ids: list[int],
    experiment_dir: Path,
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
                trial_dir=trial_dir,
                experiment_dir=experiment_dir,
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


def _resolve_experiment_output_root(
    *, experiment_dir: Path, output_base: Optional[Path]
) -> Path:
    if output_base is None:
        return experiment_dir
    return output_base / experiment_dir.name


def _resolve_trial_coverage_output_dir(
    *, trial_dir: Path, experiment_dir: Path, output_base: Optional[Path]
) -> Path:
    if output_base is None:
        return trial_dir / "coverage"
    return (
        _resolve_experiment_output_root(
            experiment_dir=experiment_dir,
            output_base=output_base,
        )
        / trial_dir.relative_to(experiment_dir)
        / "coverage"
    )


def _write_timeline_outputs(report: CoverageTimelineReport, output_dir: Path) -> None:
    """Write JSON, CSV, and PNG timeline outputs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    write_timeline_json(report, output_dir / "coverage_timeline.json")
    write_timeline_csv(report, output_dir / "coverage_timeline.csv")
    write_timeline_png(report, output_dir / "coverage_timeline.png")


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
