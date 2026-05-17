"""Shared argparse parent parsers for benchmark CI subcommands.

Each factory function returns an ArgumentParser with add_help=False,
suitable for use as a parent parser via argparse's parents= parameter.
This ensures consistent argument definitions across all subcommands.
"""

import argparse
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def create_benchmark_selection_parent() -> argparse.ArgumentParser:
    """Create parent parser for benchmark selection arguments.

    Provides:
        benchmark: Positional arg for benchmark path (e.g., "benchmarks/project_name")
        --benchmarks / -b: List of benchmark names (space-separated)
        --benchmark-suite / -s: Load benchmarks from a suite file
        --all: Run against all benchmarks
        --filter / -f: Glob pattern to filter benchmarks
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "benchmark",
        nargs="?",
        type=str,
        help="Benchmark path (e.g., benchmarks/project_name)",
    )
    parser.add_argument(
        "--benchmarks",
        "-b",
        type=str,
        nargs="+",
        help="Benchmark names (e.g., bench1 bench2 bench3)",
    )
    parser.add_argument(
        "--benchmark-suite",
        "-s",
        type=str,
        dest="benchmark_suite",
        help="Load benchmarks from a suite file (e.g., 'sanity', 'smoke/all')",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run against all benchmarks",
    )
    parser.add_argument(
        "--filter",
        "-f",
        type=str,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*')",
    )
    return parser


def create_build_options_parent() -> argparse.ArgumentParser:
    """Create parent parser for build-related arguments.

    Provides:
        --source: Source mode (pkgs or main_repo)
        --exit-on-error: Compatibility flag (accepted, currently no-op here)
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--source",
        type=str,
        choices=["pkgs", "main_repo"],
        default="pkgs",
        help="Source mode: 'pkgs' (bundled tarballs, default) or 'main_repo' (git clone)",
    )
    parser.add_argument(
        "--exit-on-error",
        action="store_true",
        help="Compatibility flag (currently no-op in modular benchmark-ci subcommands)",
    )
    parser.add_argument(
        "--mode",
        choices=["snapshot", "full"],
        default="snapshot",
        help=(
            "Patch evaluation mode: 'snapshot' reuses global cached vulnerable builds "
            "(default), 'full' disables incremental-image flow; use --force-rebuild "
            "for clean rebuilds"
        ),
    )
    parser.add_argument(
        "--force-rebuild",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Force rebuild even if cached build exists (off by default for snapshot reuse)",
    )
    parser.add_argument(
        "--max-povs-per-cpv",
        type=int,
        default=None,
        dest="max_povs_per_cpv",
        help="Limit POVs verified per CPV (e.g., 1 uses only pov_0.blob)",
    )
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="Use Redis workers for execution (default: local sequential)",
    )
    parser.add_argument(
        "--redis-host",
        type=str,
        default="localhost",
        help="Redis server hostname for distributed execution (default: localhost)",
    )
    return parser


def create_output_options_parent() -> argparse.ArgumentParser:
    """Create parent parser for output-related arguments.

    Provides:
        --output / -o: Path for results JSON
        --output-dir: Directory for detailed logs
        --no-color: Disable colored output
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Path to save results JSON",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for detailed logs",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    return parser
