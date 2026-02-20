#!/usr/bin/env python3
"""CLI entry point for benchmark statistics collection."""

import argparse
import sys
from pathlib import Path

from crsbench.statistics.collector import (
    collect_benchmark_stats,
    get_default_benchmarks_dir,
)
from crsbench.statistics.exporters import (
    export_benchmarks_csv,
    export_summary_csv,
    print_summary,
)
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def register_stats_subcommand(parent_subparsers: argparse._SubParsersAction) -> None:
    """Register 'stats' as a child subparser of an existing command group.

    Used by `crsbench benchmark stats ...` to nest stats under benchmark.

    Args:
        parent_subparsers: Subparsers action from the parent command (e.g., benchmark)
    """
    parser = parent_subparsers.add_parser(
        "stats",
        help="Collect and export benchmark statistics to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench benchmark stats --summary-only
  crsbench benchmark stats --output benchmarks.csv
  crsbench benchmark stats --benchmarks atlanta-curl-delta-01 afc-libxml2-delta-01
        """,
    )

    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=None,
        help="Path to benchmarks directory (default: auto-detect from project root)",
    )
    parser.add_argument(
        "--benchmarks",
        type=str,
        nargs="+",
        help="Specific benchmark names to process",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("benchmark_stats.csv"),
        help="Output CSV file path (default: benchmark_stats.csv)",
    )
    parser.add_argument(
        "--include-no-vulns",
        action="store_true",
        help="Include benchmarks with no vulnerabilities in output",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary only, don't export CSV",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    parser.set_defaults(func=run_stats)


def add_stats_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the stats subcommand to argparse.

    Args:
        subparsers: Subparsers action from parent ArgumentParser
    """
    parser = subparsers.add_parser(
        "stats",
        help="Collect and export benchmark statistics to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench stats --summary-only
  crsbench stats --output benchmarks.csv
  crsbench stats --benchmarks atlanta-curl-delta-01 afc-libxml2-delta-01
        """,
    )

    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=None,
        help="Path to benchmarks directory (default: auto-detect from project root)",
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        nargs="+",
        help="Specific benchmark names to process",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("benchmark_stats.csv"),
        help="Output CSV file path (default: benchmark_stats.csv)",
    )

    parser.add_argument(
        "--include-no-vulns",
        action="store_true",
        help="Include benchmarks with no vulnerabilities in output",
    )

    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary only, don't export CSV",
    )

    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    parser.set_defaults(command="stats")


def run_stats(args: argparse.Namespace) -> int:
    """Execute the stats command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Setup logging
    level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=level)

    # Parse specific benchmarks
    specific_benchmarks = args.benchmarks

    # Get benchmarks directory (auto-detect if not specified)
    benchmarks_dir = args.benchmarks_dir or get_default_benchmarks_dir()

    # Collect statistics
    logger.info(f"Collecting benchmark statistics from: {benchmarks_dir}")
    benchmarks = collect_benchmark_stats(benchmarks_dir, specific_benchmarks)

    if not benchmarks:
        logger.warning("No benchmarks found")
        return 1

    # Print summary
    print_summary(benchmarks)

    # Export CSV
    if not args.summary_only:
        export_benchmarks_csv(
            benchmarks, args.output, include_no_vulns=args.include_no_vulns
        )
        logger.info(f"CSV exported to: {args.output}")

        # Export summary CSV (auto-generate filename from output)
        summary_path = (
            args.output.parent / f"{args.output.stem}_summary{args.output.suffix}"
        )
        export_summary_csv(benchmarks, summary_path)
        logger.info(f"Summary CSV exported to: {summary_path}")

    return 0


def main() -> None:
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="Collect and export benchmark statistics to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export all benchmarks to CSV (auto-detects benchmarks directory)
  python -m crsbench.statistics.cli --output benchmarks.csv

  # Export specific benchmarks
  python -m crsbench.statistics.cli \\
    --benchmarks atlanta-curl-delta-01 afc-libxml2-delta-01 \\
    --output selected.csv

  # Summary only (no CSV export)
  python -m crsbench.statistics.cli --summary-only
        """,
    )

    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=None,
        help="Path to benchmarks directory (default: auto-detect from project root)",
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        nargs="+",
        help="Specific benchmark names to process",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("benchmark_stats.csv"),
        help="Output CSV file path (default: benchmark_stats.csv)",
    )

    parser.add_argument(
        "--include-no-vulns",
        action="store_true",
        help="Include benchmarks with no vulnerabilities in output",
    )

    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary only, don't export CSV",
    )

    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()
    sys.exit(run_stats(args))


if __name__ == "__main__":
    main()
