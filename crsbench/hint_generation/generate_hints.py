#!/usr/bin/env python3
"""CLI script to generate SARIF hints for all benchmarks.

This script scans the benchmarks directory for vuln.yaml files and generates
SARIF format hints at all levels (1-4) for each vulnerability.

Usage:
    python -m crsbench.hint_generation.generate_hints [OPTIONS]

    # Generate hints for all benchmarks
    python -m crsbench.hint_generation.generate_hints

    # Generate hints for specific benchmarks
    python -m crsbench.hint_generation.generate_hints --benchmark libxml2-delta-03

    # Specify custom benchmarks directory
    python -m crsbench.hint_generation.generate_hints --benchmarks-dir /path/to/benchmarks

    # Generate only specific hint levels
    python -m crsbench.hint_generation.generate_hints --levels 1 2 3
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from crsbench.hint_generation.sarif_generator_simple import (
    HintLevel,
    generate_hints_for_benchmark,
)
from crsbench.utils import log_summary
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def find_vuln_yaml_files(
    benchmarks_dir: Path, benchmark_names: Optional[List[str]] = None
) -> List[Path]:
    """Find all vuln.yaml files in benchmarks directory.

    Args:
        benchmarks_dir: Root benchmarks directory
        benchmark_names: Optional list of specific benchmark names to process

    Returns:
        List of paths to vuln.yaml files
    """
    vuln_yaml_files = []

    if benchmark_names:
        # Process only specified benchmarks
        for name in benchmark_names:
            benchmark_dir = benchmarks_dir / name
            if not benchmark_dir.exists():
                logger.warning(f"Benchmark directory not found: {benchmark_dir}")
                continue

            # Find vuln.yaml files in this benchmark (any structure under .aixcc/)
            files = list(benchmark_dir.glob(".aixcc/**/vuln.yaml"))
            vuln_yaml_files.extend(files)
    else:
        # Process all benchmarks (any structure under .aixcc/)
        vuln_yaml_files = list(benchmarks_dir.glob("*/.aixcc/**/vuln.yaml"))

    return vuln_yaml_files


def generate_hints_for_all_benchmarks(
    benchmarks_dir: Path,
    benchmark_names: Optional[List[str]] = None,
    levels: Optional[List[HintLevel]] = None,
    output_subdir: str = "hints",
    *,
    force_regenerate: bool = False,
) -> None:
    """Generate SARIF hints for all benchmarks with vuln.yaml files.

    Args:
        benchmarks_dir: Root benchmarks directory
        benchmark_names: Optional list of specific benchmark names
        levels: List of hint levels to generate (default: all)
        output_subdir: Subdirectory name for hint files (default: "hints")
        force_regenerate: Force regeneration even if hints exist (default: False)
    """
    if levels is None:
        levels = list(HintLevel)

    # Find all vuln.yaml files
    vuln_yaml_files = find_vuln_yaml_files(benchmarks_dir, benchmark_names)

    if not vuln_yaml_files:
        logger.warning("No vuln.yaml files found in benchmarks directory")
        return

    logger.info(f"Found {len(vuln_yaml_files)} vulnerability specifications")

    # Process each vuln.yaml file
    success_count = 0
    failure_count = 0
    skipped_count = 0

    for vuln_yaml_path in vuln_yaml_files:
        try:
            # Determine output directory: same directory as vuln.yaml
            # e.g., benchmarks/libxml2-delta-03/.aixcc/html/cpv_0/hints/
            output_dir = vuln_yaml_path.parent / output_subdir

            # Check if hints already exist (all requested levels)
            if not force_regenerate:
                all_exist = all(
                    (output_dir / f"level_{level}.sarif").exists() for level in levels
                )
                if all_exist:
                    logger.info(f"Skipping {vuln_yaml_path} (hints already exist)")
                    skipped_count += 1
                    continue

            logger.info(f"Processing: {vuln_yaml_path}")
            logger.info(f"  Output directory: {output_dir}")

            # Generate hints
            output_files = generate_hints_for_benchmark(
                vuln_yaml_path, output_dir, levels, skip_existing=not force_regenerate
            )

            # Log generated files
            for level, file_path in output_files.items():
                if not force_regenerate and file_path.exists():
                    logger.info(f"  Using existing Level {level}: {file_path}")
                else:
                    logger.info(f"  Generated Level {level}: {file_path}")

            success_count += 1

        except FileNotFoundError as e:
            logger.warning(f"Skipping {vuln_yaml_path}: {e}")
            skipped_count += 1
        except Exception:
            logger.exception("Failed to process {}", vuln_yaml_path)
            failure_count += 1

    # Summary
    log_summary(
        "Hint generation complete",
        {
            "total": len(vuln_yaml_files),
            "success": success_count,
            "skipped": skipped_count,
            "failures": failure_count,
        },
        show_percentage=False,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Generate SARIF format hints for CRSBench benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate hints for all benchmarks
  python -m crsbench.hint_generation.generate_hints

  # Generate hints for specific benchmarks
  python -m crsbench.hint_generation.generate_hints --benchmark libxml2-delta-03

  # Generate only specific hint levels
  python -m crsbench.hint_generation.generate_hints --levels 1 2

  # Use custom benchmarks directory
  python -m crsbench.hint_generation.generate_hints --benchmarks-dir /custom/path
        """,
    )

    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=None,
        help="Path to benchmarks directory (default: ./benchmarks)",
    )

    parser.add_argument(
        "--benchmark",
        "--benchmarks",
        dest="benchmarks",
        action="append",
        help="Specific benchmark(s) to process (can specify multiple times)",
    )

    parser.add_argument(
        "--levels",
        type=int,
        nargs="+",
        choices=[1, 2, 3, 4, 5],
        help="Hint levels to generate (default: all levels 1-5)",
    )

    parser.add_argument(
        "--output-subdir",
        type=str,
        default="hints",
        help="Subdirectory name for hint files (default: hints)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force regeneration even if hint files already exist",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    args = parse_args()

    # Set logging level
    if args.verbose:
        configure_logger(level="DEBUG")

    # Determine benchmarks directory
    if args.benchmarks_dir:
        benchmarks_dir = args.benchmarks_dir
    else:
        # Default to ./benchmarks relative to current working directory
        benchmarks_dir = Path.cwd() / "benchmarks"

    if not benchmarks_dir.exists():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    logger.info(f"Using benchmarks directory: {benchmarks_dir}")

    # Convert level integers to HintLevel enum
    levels = None
    if args.levels:
        levels = [HintLevel(level) for level in args.levels]
        logger.info(f"Generating hints for levels: {args.levels}")

    # Generate hints
    try:
        generate_hints_for_all_benchmarks(
            benchmarks_dir=benchmarks_dir,
            benchmark_names=args.benchmarks,
            levels=levels,
            output_subdir=args.output_subdir,
            force_regenerate=args.force,
        )
        return 0
    except Exception:
        logger.exception("Hint generation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
