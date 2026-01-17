"""Benchmark runtime loading for CRS evaluation.

This module provides tools to load benchmark source code at runtime,
handling both bundled (pkgs/) and git-cloned sources.

Key components:
- loader: Load benchmark source with automatic detection
- models: BenchmarkSource dataclass

For verification (verify/patch-verify), use prepare_source_from_bundle()
to extract and prepare source from pkgs/ with optional ref.diff application.
"""

from crsbench.benchmark.runtime.loader import (
    get_ref_diff_path,
    has_bundled_source,
    load_benchmark_source,
    prepare_source_from_bundle,
)
from crsbench.benchmark.runtime.models import BenchmarkSource

__all__ = [
    "load_benchmark_source",
    "has_bundled_source",
    "prepare_source_from_bundle",
    "get_ref_diff_path",
    "BenchmarkSource",
]
