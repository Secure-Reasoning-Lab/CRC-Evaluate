#!/usr/bin/env python3
"""
Bundle All Benchmarks - Create pkgs/ tarballs for all benchmarks.

This utility script bundles all benchmarks in the benchmarks directory,
creating pkgs/ directories with source tarballs and ref.diff files.

Usage:
    # Bundle all benchmarks
    python scripts/bundle_all_benchmarks.py

    # Bundle specific benchmarks
    python scripts/bundle_all_benchmarks.py --benchmarks afc-curl-delta-01,afc-tinyxml2-delta-01

    # Bundle benchmarks matching pattern
    python scripts/bundle_all_benchmarks.py --pattern "afc-*-delta-*"

    # Force overwrite existing pkgs/
    python scripts/bundle_all_benchmarks.py --force

    # Dry run (show what would be bundled)
    python scripts/bundle_all_benchmarks.py --dry-run

    # Custom benchmarks directory
    python scripts/bundle_all_benchmarks.py --benchmarks-dir ./custom-benchmarks

Examples:
    # Bundle all AFC benchmarks
    $ python scripts/bundle_all_benchmarks.py --pattern "afc-*"

    # Bundle specific set, force overwrite
    $ python scripts/bundle_all_benchmarks.py \
        --benchmarks afc-curl-delta-01,afc-json-c-delta-01 \
        --force

    # Preview what would be bundled
    $ python scripts/bundle_all_benchmarks.py --dry-run
"""

import argparse
import fnmatch
import sys
from pathlib import Path

from crsbench.benchmark.packaging import bundle_benchmark, validate_benchmark
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def find_benchmarks(
    benchmarks_dir: Path, pattern: str | None = None
) -> list[Path]:
    """Find all benchmark directories.

    Args:
        benchmarks_dir: Root directory containing benchmarks
        pattern: Optional glob pattern to filter benchmarks

    Returns:
        List of benchmark directory paths
    """
    benchmarks = []

    for path in benchmarks_dir.iterdir():
        if not path.is_dir():
            continue
        # Skip hidden directories
        if path.name.startswith("."):
            continue
        # Check if it looks like a benchmark (has Dockerfile or project.yaml)
        if not (path / "Dockerfile").exists() and not (path / "project.yaml").exists():
            continue
        # Apply pattern filter if specified
        if pattern and not fnmatch.fnmatch(path.name, pattern):
            continue
        benchmarks.append(path)

    return sorted(benchmarks)


def bundle_all(
    benchmarks: list[Path],
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, str]:
    """Bundle all benchmarks.

    Args:
        benchmarks: List of benchmark paths to bundle
        force: Overwrite existing pkgs/ directories
        dry_run: Only show what would be done

    Returns:
        Dictionary mapping benchmark names to status ("ok", "skipped", "failed", "dry-run")
    """
    results: dict[str, str] = {}

    for benchmark_path in benchmarks:
        name = benchmark_path.name

        if dry_run:
            pkgs_dir = benchmark_path / "pkgs"
            if pkgs_dir.exists():
                logger.info(f"[dry-run] Would overwrite: {name}")
            else:
                logger.info(f"[dry-run] Would bundle: {name}")
            results[name] = "dry-run"
            continue

        # Check if already bundled
        pkgs_dir = benchmark_path / "pkgs"
        if pkgs_dir.exists() and not force:
            logger.info(f"Skipping (already bundled): {name}")
            results[name] = "skipped"
            continue

        # Validate before bundling
        validation = validate_benchmark(benchmark_path)
        if not validation.valid:
            logger.error(f"Validation failed for {name}: {validation.errors}")
            results[name] = "failed"
            continue

        try:
            logger.info(f"Bundling: {name}")
            bundle_benchmark(benchmark_path, force=force)
            logger.info(f"Successfully bundled: {name}")
            results[name] = "ok"
        except Exception as e:
            logger.error(f"Failed to bundle {name}: {e}")
            results[name] = "failed"

    return results


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Bundle all benchmarks to create pkgs/ directories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        help="Comma-separated list of benchmark names to bundle",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        help="Glob pattern to filter benchmarks (e.g., 'afc-*-delta-*')",
    )
    parser.add_argument(
        "--benchmarks-dir",
        type=str,
        default="./benchmarks",
        help="Path to benchmarks directory (default: ./benchmarks)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing pkgs/ directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be bundled without actually bundling",
    )

    args = parser.parse_args()

    benchmarks_dir = Path(args.benchmarks_dir)
    if not benchmarks_dir.exists():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    # Find benchmarks
    if args.benchmarks:
        # Specific benchmarks
        benchmark_names = [n.strip() for n in args.benchmarks.split(",")]
        benchmarks = []
        for name in benchmark_names:
            path = benchmarks_dir / name
            if path.exists():
                benchmarks.append(path)
            else:
                logger.warning(f"Benchmark not found: {name}")
    else:
        # All benchmarks (with optional pattern)
        benchmarks = find_benchmarks(benchmarks_dir, args.pattern)

    if not benchmarks:
        logger.error("No benchmarks found")
        return 1

    logger.info(f"Found {len(benchmarks)} benchmark(s) to process")

    # Bundle all
    results = bundle_all(
        benchmarks,
        force=args.force,
        dry_run=args.dry_run,
    )

    # Summary
    ok = sum(1 for v in results.values() if v == "ok")
    skipped = sum(1 for v in results.values() if v == "skipped")
    failed = sum(1 for v in results.values() if v == "failed")
    dry_run_count = sum(1 for v in results.values() if v == "dry-run")

    logger.info("=" * 50)
    if args.dry_run:
        logger.info(f"Dry run complete: {dry_run_count} benchmark(s) would be bundled")
    else:
        logger.info(f"Bundling complete: {ok} ok, {skipped} skipped, {failed} failed")

    if failed > 0:
        failed_names = [k for k, v in results.items() if v == "failed"]
        logger.error(f"Failed benchmarks: {', '.join(failed_names)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
