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
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import yaml

from crsbench.evaluation.coverage.builder import CoverageBuilder
from crsbench.evaluation.coverage.models import CoverageConfig, CoverageSummary
from crsbench.evaluation.coverage.store import CoverageStore
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategyError,
    create_coverage_strategy,
    parse_llvm_cov_summary,
)
from crsbench.utils.logger import get_logger

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
        "--oss-fuzz",
        type=Path,
        default=None,
        help="Path to oss-fuzz directory (default: ./oss-fuzz)",
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

    parser.set_defaults(func=run_coverage)


def run_coverage(args: argparse.Namespace) -> int:
    """Execute the coverage command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Validate benchmark path
    if not args.benchmark_path.exists():
        logger.error(f"Benchmark path not found: {args.benchmark_path}")
        return 1

    # Validate corpus directory
    if not args.corpus_dir.exists():
        logger.error(f"Corpus directory not found: {args.corpus_dir}")
        return 1

    corpus_files = list(args.corpus_dir.iterdir())
    if not corpus_files:
        logger.error(f"Corpus directory is empty: {args.corpus_dir}")
        return 1

    # Determine oss-fuzz path
    oss_fuzz_path = args.oss_fuzz or Path("./oss-fuzz")
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    logger.info(f"Collecting coverage for benchmark: {args.benchmark_path}")
    logger.info(f"Corpus directory: {args.corpus_dir} ({len(corpus_files)} files)")

    # Load benchmark configuration
    benchmark_config = load_benchmark_config(args.benchmark_path)
    if not benchmark_config:
        return 1

    project_name = args.benchmark_path.name
    language = benchmark_config.get("language", "c")
    main_repo = benchmark_config.get("main_repo", "")
    commit = get_target_commit(args.benchmark_path, benchmark_config)

    if not commit:
        logger.error("Could not determine target commit from benchmark")
        return 1

    # Determine harness name
    harness_name = args.harness or get_first_harness(args.benchmark_path)
    if not harness_name:
        logger.error("Could not determine harness name. Use --harness to specify.")
        return 1

    logger.info(f"Harness: {harness_name}")
    logger.info(f"Language: {language}")
    logger.info(f"Commit: {commit[:12]}")

    # Build coverage variant
    builder = CoverageBuilder(oss_fuzz_path)
    coverage_build = builder.build(
        project_name=project_name,
        benchmark_path=args.benchmark_path,
        main_repo=main_repo,
        commit=commit,
        language=language,
        force_rebuild=args.force_rebuild,
    )

    if not coverage_build:
        logger.error("Failed to build coverage variant")
        return 1

    # Collect coverage
    try:
        strategy = create_coverage_strategy(
            oss_fuzz_path=oss_fuzz_path,
            project_name=coverage_build.variant_name,
            language=language,
        )

        logger.info(f"Running coverage collection for {harness_name}...")
        summary_path = strategy.collect_batch_coverage(
            harness_path=Path(harness_name),
            corpus_dir=args.corpus_dir,
        )

        # Parse coverage summary
        cov_stats = parse_llvm_cov_summary(summary_path)
        summary = CoverageSummary(
            metric="line",
            corpus_total=len(corpus_files),
            corpus_contributing=len(corpus_files),  # All contribute in batch mode
            lines_covered=int(cov_stats.get("lines_covered", 0)),
            lines_total=int(cov_stats.get("lines_total", 0)),
            lines_percent=float(cov_stats.get("lines_percent", 0.0)),
            functions_covered=int(cov_stats.get("functions_covered", 0)),
            functions_total=int(cov_stats.get("functions_total", 0)),
        )

        # Output results
        output_results(summary, harness_name, args.output, args.format)

        # Print summary
        print_summary(summary, harness_name)

        return 0

    except CoverageStrategyError as e:
        logger.error(f"Coverage collection failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 1


def load_benchmark_config(benchmark_path: Path) -> Optional[dict]:
    """Load benchmark configuration from project.yaml.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Configuration dict or None if loading fails
    """
    project_yaml = benchmark_path / "project.yaml"
    if not project_yaml.exists():
        logger.error(f"project.yaml not found: {project_yaml}")
        return None

    try:
        with project_yaml.open() as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load project.yaml: {e}")
        return None


def get_target_commit(benchmark_path: Path, config: dict) -> Optional[str]:
    """Get target commit for coverage collection.

    For delta mode, uses ref_commit (vulnerable version).
    For full mode, uses base_commit.

    Args:
        benchmark_path: Path to benchmark directory
        config: Project configuration dict

    Returns:
        Commit hash or None if not found
    """
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_yaml.exists():
        logger.error(f"meta.yaml not found: {meta_yaml}")
        return None

    try:
        with meta_yaml.open() as f:
            meta = yaml.safe_load(f)

        # Check for ref_commit (delta mode) or base_commit (full mode)
        ref_commit = meta.get("ref_commit")
        base_commit = meta.get("base_commit")

        # Prefer ref_commit for delta mode (vulnerable version)
        return ref_commit or base_commit

    except Exception as e:
        logger.error(f"Failed to load meta.yaml: {e}")
        return None


def get_first_harness(benchmark_path: Path) -> Optional[str]:
    """Get the first available harness name from benchmark.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Harness name or None if not found
    """
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_yaml.exists():
        return None

    try:
        with meta_yaml.open() as f:
            meta = yaml.safe_load(f)

        harnesses = meta.get("harnesses", [])
        if harnesses and isinstance(harnesses, list):
            first_harness = harnesses[0]
            if isinstance(first_harness, dict):
                return first_harness.get("name")
            return str(first_harness)

        return None

    except Exception as e:
        logger.warning(f"Failed to get harness from meta.yaml: {e}")
        return None


def output_results(
    summary: CoverageSummary,
    harness_name: str,
    output_path: Optional[Path],
    output_format: str,
) -> None:
    """Output coverage results.

    Args:
        summary: Coverage summary
        harness_name: Name of the harness
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
            f"Lines Covered: {summary.lines_covered}/{summary.lines_total} "
            f"({summary.lines_percent:.1f}%)\n"
            f"Functions Covered: {summary.functions_covered}/{summary.functions_total}\n"
            f"Corpus Files: {summary.corpus_total}"
        )

    if output_path:
        output_path.write_text(output)
        logger.info(f"Results written to: {output_path}")
    else:
        print(output)


def print_summary(summary: CoverageSummary, harness_name: str) -> None:
    """Print a summary of coverage results.

    Args:
        summary: Coverage summary
        harness_name: Name of the harness
    """
    print("\n" + "=" * 50)
    print("COVERAGE SUMMARY")
    print("=" * 50)
    print(f"Harness: {harness_name}")
    print(
        f"Lines: {summary.lines_covered}/{summary.lines_total} "
        f"({summary.lines_percent:.1f}%)"
    )
    print(f"Functions: {summary.functions_covered}/{summary.functions_total}")
    print(f"Corpus files: {summary.corpus_total}")
    print("=" * 50)


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
