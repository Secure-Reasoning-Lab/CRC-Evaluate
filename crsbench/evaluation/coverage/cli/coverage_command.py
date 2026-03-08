"""CLI command for coverage collection.

This module provides the `crsbench coverage` CLI command for collecting
code coverage from corpus files against benchmark projects.

Usage:
    crsbench coverage <benchmark_path> --corpus-dir <dir> [options]

Examples:
    # Collect coverage for a benchmark
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/

    # Collect coverage with specific harness
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --harness fuzz_parse

    # Output results to JSON file
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --output report.json

    # Parallel execution with workers
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ \
        --build-workers 4 --verify-workers 8
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from crsbench.evaluation.coverage.engine import CoverageEngine
from crsbench.evaluation.coverage.models import CoverageSummary
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

logger = get_logger(__name__)


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
            "corpus files against it to measure code coverage."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Collect coverage for a benchmark
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/

  # Collect coverage with specific harness
  crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/ --harness fuzz_parse

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
        help="Path to the benchmark project directory",
    )

    parser.add_argument(
        "--corpus-dir",
        type=Path,
        required=True,
        help="Directory containing corpus files to measure coverage",
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
        help="Output file path for results",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="json",
        choices=["json", "yaml", "text"],
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--build-workers",
        type=int,
        default=None,
        help="Number of parallel workers for building coverage variants (default: 4).",
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=None,
        help="Number of parallel workers for coverage collection (default: 4).",
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

    Uses CoverageEngine for coverage collection, following the same patterns
    as VerificationEngine (POV) and PatchVerificationEngine. Supports parallel
    execution with build_workers and verify_workers.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Configure logging
    from crsbench.utils.logger import configure_logger

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=log_level)

    # Validate benchmark path
    if not args.benchmark_path.exists():
        logger.error(f"Benchmark path not found: {args.benchmark_path}")
        return 1

    # Validate corpus directory
    if not args.corpus_dir.exists():
        logger.error(f"Corpus directory not found: {args.corpus_dir}")
        return 1

    corpus_files = [f for f in args.corpus_dir.iterdir() if f.is_file()]
    if not corpus_files:
        logger.error(f"Corpus directory is empty: {args.corpus_dir}")
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
    logger.info(f"Corpus directory: {args.corpus_dir} ({len(corpus_files)} files)")

    # Create engine and collect coverage
    # Note: CoverageEngine processes corpus sequentially; verify_workers
    # is used by Redis job queue for benchmark-level parallelism
    engine = CoverageEngine(
        oss_fuzz_path=oss_fuzz_path,
        build_workers=args.build_workers,
        source_mode=args.source,
    )

    try:
        report = engine.collect_coverage(
            benchmark_path=args.benchmark_path,
            corpus_dir=args.corpus_dir,
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
