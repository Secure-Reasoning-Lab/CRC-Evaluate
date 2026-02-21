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

import yaml

from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import BenchmarkSuiteConfig

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


def get_suites_root() -> Path:
    """Get the root benchmark-suites directory.

    Resolution order:
    1. BENCHMARK_SUITES_ROOT environment variable
    2. Walk parent directories looking for benchmark-suites/ subdirectory

    Raises:
        RuntimeError: If benchmark-suites directory cannot be found
    """
    if "BENCHMARK_SUITES_ROOT" in os.environ:
        return Path(os.environ["BENCHMARK_SUITES_ROOT"])

    current = Path(__file__).resolve()
    for parent in current.parents:
        suites_dir = parent / "benchmark-suites"
        if suites_dir.is_dir():
            return suites_dir

    raise RuntimeError("Could not find benchmark-suites directory")


def load_benchmark_suite(suite_name: str) -> list[str]:
    """Load benchmark names from a suite file.

    Uses BenchmarkSuiteConfig schema for validation.

    Args:
        suite_name: Suite name (e.g., 'smoke-test-bug-finding') or path to suite file

    Returns:
        List of benchmark names from the suite

    Raises:
        SystemExit: If suite file not found or invalid
    """
    suite_path = Path(suite_name)

    # If not absolute, try to find in suites directory
    if not suite_path.is_absolute():
        if not suite_path.suffix:
            suite_path = suite_path.with_suffix(".yaml")

        if not suite_path.exists():
            suites_root = get_suites_root()
            suite_path = suites_root / suite_path

    if not suite_path.exists():
        logger.error(f"Benchmark suite not found: {suite_path}")
        raise SystemExit(1)

    # Load and validate using existing schema
    with suite_path.open() as f:
        data = yaml.safe_load(f)

    suite_config = BenchmarkSuiteConfig(**data)
    benchmark_names = suite_config.get_benchmark_names()

    logger.info(
        f"Loaded suite '{suite_config.Name}': {len(benchmark_names)} benchmarks"
    )
    return benchmark_names


def _resolve_single_benchmark(benchmark_arg: str, benchmarks_root: Path) -> Path:
    """Resolve a single benchmark argument to a path.

    Args:
        benchmark_arg: Benchmark name or path
        benchmarks_root: Root directory containing benchmarks

    Returns:
        Resolved benchmark path

    Raises:
        SystemExit: If path not found
    """
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
    return resolved


def resolve_benchmark_paths(
    benchmark_arg: Optional[str] = None,
    *,
    benchmarks_list: Optional[list[str]] = None,
    benchmark_suite: Optional[str] = None,
    all_benchmarks: bool = False,
    filter_pattern: Optional[str] = None,
) -> list[Path]:
    """Resolve benchmark paths from CLI arguments.

    Handles multiple input formats:
    - Positional arg: single benchmark name or path
    - --benchmarks: list of benchmark names
    - --benchmark-suite: load benchmarks from a suite file
    - --all: discover all benchmarks with optional filter

    Args:
        benchmark_arg: Positional benchmark argument (name or path)
        benchmarks_list: List of benchmark names (--benchmarks)
        benchmark_suite: Name or path to a benchmark suite file (--benchmark-suite)
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

    if benchmark_suite:
        names = load_benchmark_suite(benchmark_suite)
        return [_resolve_single_benchmark(name, benchmarks_root) for name in names]

    if benchmarks_list:
        names = [name.strip() for name in benchmarks_list if name.strip()]
        if not names:
            logger.error("Empty --benchmarks list provided")
            raise SystemExit(1)
        return [_resolve_single_benchmark(name, benchmarks_root) for name in names]

    if benchmark_arg:
        return [_resolve_single_benchmark(benchmark_arg, benchmarks_root)]

    logger.error(
        "No benchmarks specified. Use positional arg, --benchmarks, "
        "--benchmark-suite, or --all"
    )
    raise SystemExit(1)
