"""CLI entry point for benchmark CI testing.

Usage:
    crsbench ci --benchmarks bench1,bench2

Or via module:
    python -m crsbench.benchmark_ci.cli.main --benchmarks bench1,bench2
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Set

from crsbench.benchmark_ci.models import ExecJobType, get_benchmarks_root
from crsbench.benchmark_ci.runner import BenchmarkCIRunner
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def add_ci_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add 'ci' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    ci_parser = subparsers.add_parser(
        "ci",
        help="Run benchmark CI tests (POV checks, patch verification, coverage)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test specific benchmarks
  %(prog)s --benchmarks sanity-mock-c-delta-01,sanity-mock-c-full-01

  # Test all benchmarks
  %(prog)s --all

  # Run specific job types only
  %(prog)s --benchmarks bench1 --job-types delta_base_pov_check,patch_check

  # Run with parallel workers
  %(prog)s --all --workers 4

  # Export results to CSV
  %(prog)s --all --csv results.csv

  # Dry run - show jobs without executing
  %(prog)s --benchmarks bench1 --dry-run
        """,
    )

    # Benchmark selection
    ci_parser.add_argument(
        "--benchmarks",
        "-b",
        type=str,
        help="Comma-separated list of benchmark names to test",
    )
    ci_parser.add_argument(
        "--all",
        action="store_true",
        help="Test all benchmarks in the benchmarks/ directory",
    )

    # Job type filtering
    ci_parser.add_argument(
        "--job-types",
        "-j",
        type=str,
        help="Comma-separated list of job types to run (e.g., delta_base_pov_check,patch_check)",
    )

    # Output
    ci_parser.add_argument(
        "--csv",
        type=str,
        help="Path to export results CSV",
    )

    # Parallel execution
    ci_parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=1,
        help="Number of parallel workers for job execution (default: 1)",
    )

    # Options
    ci_parser.add_argument(
        "--check-default-only",
        action="store_true",
        help="Only test libfuzzer + address/none sanitizers",
    )
    ci_parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild Docker images even if they already exist",
    )
    ci_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show jobs that would be executed without running them",
    )

    ci_parser.set_defaults(command="ci")


def get_benchmarks_to_test(args: argparse.Namespace) -> Set[str]:
    """Get set of benchmarks to test based on CLI arguments."""
    benchmarks: Set[str] = set()

    if args.all:
        # Get all benchmarks from benchmarks/ directory
        benchmarks_root = Path(get_benchmarks_root())
        for path in benchmarks_root.iterdir():
            if path.is_dir() and not path.name.startswith("."):
                benchmarks.add(path.name)
    elif args.benchmarks:
        benchmarks = set(args.benchmarks.split(","))
    else:
        logger.error("No benchmarks specified. Use --benchmarks or --all")
        sys.exit(1)

    return benchmarks


def parse_job_types(job_types_str: Optional[str]) -> Optional[Set[ExecJobType]]:
    """Parse job types from comma-separated string."""
    if not job_types_str:
        return None

    job_types: Set[ExecJobType] = set()
    for jt in job_types_str.split(","):
        jt = jt.strip()
        try:
            job_types.add(ExecJobType(jt))
        except ValueError:
            logger.warning(f"Unknown job type: {jt}")

    return job_types if job_types else None


def run_ci(args: argparse.Namespace) -> int:
    """Run benchmark CI tests.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Get benchmarks to test
    benchmarks = get_benchmarks_to_test(args)
    logger.info(f"Testing {len(benchmarks)} benchmarks: {sorted(benchmarks)}")

    # Parse job types
    job_types = parse_job_types(args.job_types)
    if job_types:
        logger.info(f"Running job types: {[jt.value for jt in job_types]}")

    # Create runner
    runner = BenchmarkCIRunner(
        force_rebuild=args.force_rebuild,
        max_workers=args.workers,
    )

    # Dry run - just show jobs
    if args.dry_run:
        jobs = runner.generate_jobs(
            benchmarks,
            job_types,
            check_default_only=args.check_default_only,
        )
        logger.info(f"Would execute {len(jobs)} jobs:")
        for i, job in enumerate(jobs, 1):
            inc_build_str = " [inc-build]" if job.use_inc_build else ""
            logger.info(f"  {i}. {job}{inc_build_str}")
        return 0

    # Run tests
    results = runner.run(
        benchmarks,
        job_types,
        check_default_only=args.check_default_only,
    )

    # Print summary
    results.print_summary()

    # Export CSV if requested
    if args.csv:
        results.export_csv(args.csv)

    # Return exit code based on results
    summary = results.get_summary()
    if summary["failed"] > 0:
        return 1
    return 0


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments (for standalone execution)."""
    parser = argparse.ArgumentParser(
        description="Benchmark CI testing for CRSBench",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Create subparsers and add ci command
    subparsers = parser.add_subparsers(dest="command")
    add_ci_subparser(subparsers)

    # For standalone execution, parse without subcommand requirement
    parsed = parser.parse_args(args)

    # If no command specified, show help
    if parsed.command is None:
        # Re-parse with ci as default command
        if args is None:
            args = sys.argv[1:]
        return parser.parse_args(["ci"] + list(args))

    return parsed


def main(args: Optional[List[str]] = None) -> int:
    """Main entry point for benchmark CI CLI."""
    parsed_args = parse_args(args)
    return run_ci(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
