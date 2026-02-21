"""Retry failed benchmarks from a previous CI run.

Reads a CSV file from a previous CI run and re-runs only the benchmarks
that failed (FAIL or ERR status).
"""

import argparse
import csv
from pathlib import Path

from crsbench.benchmark_ci.cli.commands.all_cmd import run_all
from crsbench.benchmark_ci.cli.common_args import (
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import get_benchmarks_root
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_csv_for_failed_benchmarks(csv_path: Path) -> list[str]:
    """Parse CSV and return list of failed benchmark names.

    Looks for rows where the 'total' column is FAIL or ERR.

    Args:
        csv_path: Path to summary.csv file

    Returns:
        List of benchmark names that failed
    """
    failed_benchmarks = []

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)

        # Find the column that contains the total status
        # Different check modes may have different column names
        # but 'total' should be present in ALL mode CSV
        for row in reader:
            benchmark = row.get("benchmark", "")
            if not benchmark:
                continue

            # Check 'total' column for status (case-insensitive)
            total_status = row.get("total", "").upper()
            if total_status in ("FAIL", "ERR", "ERROR"):
                failed_benchmarks.append(benchmark)
                continue

            # Also check 'total_status' for older format
            total_status_alt = row.get("total_status", "").upper()
            if total_status_alt in ("FAIL", "ERR", "ERROR"):
                failed_benchmarks.append(benchmark)

    return failed_benchmarks


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the retry subcommand."""
    parser = subparsers.add_parser(
        "retry",
        parents=[
            create_build_options_parent(),
            create_output_options_parent(),
        ],
        help="Retry failed benchmarks from a previous CI run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Retry failed benchmarks from a previous run
  crsbench benchmark ci retry --csv ./ci-output/summary.csv --output-dir ./ci-retry

  # Retry using bundled source
  crsbench benchmark ci retry --csv ./ci-output/summary.csv --source pkgs

  # Retry with different output directory
  crsbench benchmark ci retry --csv ./previous-run/summary.csv --output-dir ./retry-output
        """,
    )
    parser.add_argument(
        "--csv",
        type=str,
        required=True,
        help="Path to summary.csv from a previous CI run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which benchmarks would be retried without running them",
    )
    parser.set_defaults(ci_func=run_retry)


def run_retry(args: argparse.Namespace) -> int:
    """Run retry command - re-run failed benchmarks from CSV.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    csv_path = Path(args.csv)
    if not csv_path.exists():
        logger.error(f"CSV file not found: {csv_path}")
        return 1

    # Parse CSV to find failed benchmarks
    failed_benchmarks = _parse_csv_for_failed_benchmarks(csv_path)

    if not failed_benchmarks:
        logger.info("No failed benchmarks found in CSV. Nothing to retry.")
        return 0

    logger.info(f"Found {len(failed_benchmarks)} failed benchmark(s) to retry:")
    for bench in failed_benchmarks:
        logger.info(f"  - {bench}")

    # Dry run mode - just show what would be retried
    if getattr(args, "dry_run", False):
        logger.info("Dry run mode - not executing retries")
        return 0

    # Verify all benchmarks exist
    benchmarks_root = get_benchmarks_root()
    missing = []
    for bench in failed_benchmarks:
        bench_path = benchmarks_root / bench
        if not bench_path.exists():
            missing.append(bench)

    if missing:
        logger.error(f"Some benchmarks not found: {missing}")
        return 1

    # Create a modified args object for run_all
    # We need to set the benchmark selection to use our failed list
    retry_args = argparse.Namespace(**vars(args))
    retry_args.benchmark = None
    retry_args.benchmarks = failed_benchmarks
    retry_args.benchmark_suite = None
    retry_args.all = False
    retry_args.filter = None

    logger.info(f"Retrying {len(failed_benchmarks)} failed benchmark(s)...")

    # Run the all command with the failed benchmarks
    return run_all(retry_args)
