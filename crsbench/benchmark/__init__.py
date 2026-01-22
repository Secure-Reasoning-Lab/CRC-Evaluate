"""Benchmark lifecycle management.

This module provides tools for the complete benchmark lifecycle:

- **generation**: Create new benchmarks from various sources (future)
- **packaging**: Bundle benchmarks for distribution (tarballs, validation)
- **runtime**: Load benchmarks for CRS evaluation
"""

from crsbench.benchmark.packaging import (
    ValidationResult,
    bundle_benchmark,
    create_source_tarball,
    get_expected_source_dir,
    validate_benchmark,
)
from crsbench.benchmark.runtime import BenchmarkSource, load_benchmark_source

__all__ = [
    # Packaging
    "bundle_benchmark",
    "create_source_tarball",
    "get_expected_source_dir",
    "validate_benchmark",
    "ValidationResult",
    # Runtime
    "load_benchmark_source",
    "BenchmarkSource",
]
