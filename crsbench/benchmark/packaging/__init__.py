"""Benchmark packaging tools for distribution.

This module provides tools to package benchmark source code into distributable
tarballs with fresh git history, supporting both full and delta modes.

Key components:
- workdir_parser: Parse WORKDIR from Dockerfile
- tarball: Create source tarballs with fresh git init
- validate: Validate benchmark structure
- bundle: High-level bundling interface
"""

from crsbench.benchmark.packaging.bundle import bundle_benchmark
from crsbench.benchmark.packaging.tarball import create_source_tarball
from crsbench.benchmark.packaging.validate import ValidationResult, validate_benchmark
from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir

__all__ = [
    "bundle_benchmark",
    "create_source_tarball",
    "get_expected_source_dir",
    "validate_benchmark",
    "ValidationResult",
]
