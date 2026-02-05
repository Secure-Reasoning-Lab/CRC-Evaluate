"""Benchmark utility functions for filtering and mode detection.

This module provides shared utilities for working with benchmarks across
orchestrator, worker, and evaluator components.
"""

from pathlib import Path
from typing import List

import yaml

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_available_modes_for_benchmark(benchmark_path: Path) -> List[str]:
    """Get list of available evaluation modes (delta/full) for a benchmark.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        List of available mode strings ('delta' and/or 'full')
    """
    meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_yaml_path.exists():
        logger.warning(f"meta.yaml not found at {meta_yaml_path}")
        return []

    try:
        with meta_yaml_path.open() as f:
            meta_data = yaml.safe_load(f)

        modes = []
        if meta_data.get("delta_mode"):
            modes.append("delta")
        if meta_data.get("full_mode"):
            modes.append("full")

        return modes
    except Exception as e:
        logger.warning(f"Failed to read modes from {meta_yaml_path}: {e}")
        return []


def filter_benchmarks_by_mode(
    benchmark_names: List[str],
    mode: str,
    benchmarks_root: Path,
) -> List[str]:
    """Filter benchmarks to only those supporting the specified mode.

    This function is used by orchestrator, worker, and evaluator to filter
    benchmarks early, before resolving harnesses or generating trials.

    Args:
        benchmark_names: List of benchmark names to filter
        mode: Target mode ('delta', 'full', or 'all')
        benchmarks_root: Root directory containing benchmark directories

    Returns:
        Filtered list of benchmark names that support the specified mode
    """
    if mode == "all":
        return benchmark_names

    filtered = []
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        available_modes = get_available_modes_for_benchmark(benchmark_path)
        if mode in available_modes:
            filtered.append(name)

    return filtered
