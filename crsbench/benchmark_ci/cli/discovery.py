"""Benchmark path resolution and discovery.

Handles three input formats for benchmark specification:
- Bare name: "my-project" -> resolved via benchmarks_root
- Relative path: "benchmarks/my-project" -> resolved from CWD
- Absolute path: "/path/to/my-project" -> used directly

Also provides discovery of all benchmarks via .aixcc marker directories.
"""

import fnmatch
import os
from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_benchmarks_root() -> Path:
    """Get the root benchmarks directory.

    Resolution order:
    1. BENCHMARKS_ROOT environment variable
    2. Walk parent directories looking for benchmarks/ subdirectory

    Raises:
        RuntimeError: If benchmarks directory cannot be found
    """
    if "BENCHMARKS_ROOT" in os.environ:
        return Path(os.environ["BENCHMARKS_ROOT"])

    current = Path(__file__).resolve()
    for parent in current.parents:
        benchmarks_dir = parent / "benchmarks"
        if benchmarks_dir.is_dir():
            return benchmarks_dir

    raise RuntimeError("Could not find benchmarks directory")


def discover_benchmarks(
    benchmarks_root: Path, filter_pattern: Optional[str] = None
) -> list[Path]:
    """Discover benchmark directories with .aixcc marker.

    Scans benchmarks_root for subdirectories containing a .aixcc directory,
    skipping hidden directories and applying optional glob filter.

    Args:
        benchmarks_root: Root directory containing benchmarks
        filter_pattern: Optional glob pattern to filter benchmark names

    Returns:
        Sorted list of benchmark paths
    """
    benchmarks = []
    for path in sorted(benchmarks_root.iterdir()):
        if not path.is_dir():
            continue
        if path.name.startswith("."):
            continue
        if not (path / ".aixcc").exists():
            continue
        if filter_pattern and not fnmatch.fnmatch(path.name, filter_pattern):
            continue
        benchmarks.append(path)
    return benchmarks


def resolve_benchmark_paths(
    benchmark_arg: Optional[str] = None,
    *,
    all_benchmarks: bool = False,
    filter_pattern: Optional[str] = None,
) -> list[Path]:
    """Resolve benchmark paths from CLI arguments.

    Handles three input formats:
    - Absolute path: use directly
    - Contains "/": resolve relative to CWD
    - Bare name: resolve via benchmarks_root

    Args:
        benchmark_arg: Positional benchmark argument (name or path)
        all_benchmarks: Whether --all was specified
        filter_pattern: Optional glob filter for --all mode

    Returns:
        List of resolved benchmark paths

    Raises:
        SystemExit: If no benchmarks specified or path not found
    """
    benchmarks_root = get_benchmarks_root()

    if all_benchmarks:
        benchmarks = discover_benchmarks(benchmarks_root, filter_pattern)
        if not benchmarks:
            logger.error("No benchmarks found matching criteria")
            raise SystemExit(1)
        return benchmarks

    if benchmark_arg:
        path = Path(benchmark_arg)

        if path.is_absolute():
            resolved = path
        elif "/" in benchmark_arg:
            resolved = Path.cwd() / path
        else:
            resolved = benchmarks_root / benchmark_arg

        if not resolved.exists():
            logger.error(f"Benchmark not found: {resolved}")
            raise SystemExit(1)
        return [resolved]

    logger.error("No benchmarks specified. Use positional arg or --all")
    raise SystemExit(1)
