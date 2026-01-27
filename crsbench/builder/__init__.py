"""Unified builder module for OSS-Fuzz projects.

This module provides a unified interface for building OSS-Fuzz project variants:
- Validation variants (deltabase, deltaref, allpatched, cpvN)
- Coverage variants (coverage-instrumented builds)

Main classes:
- OSSFuzzBuilder: Main builder class with parallel support
- BuildConfig: Configuration for a single build
- BuildResult: Result of a build operation
- VariantType: Enum for variant types
"""

from crsbench.builder.builder import OSSFuzzBuilder
from crsbench.builder.infrastructure import OSSFuzzInfrastructure, ensure_oss_fuzz_ready
from crsbench.builder.types import (
    BenchmarkMode,
    BuildConfig,
    BuildPlan,
    BuildResult,
    VariantType,
)

__all__ = [
    "BenchmarkMode",
    "BuildConfig",
    "BuildPlan",
    "BuildResult",
    "OSSFuzzBuilder",
    "OSSFuzzInfrastructure",
    "VariantType",
    "ensure_oss_fuzz_ready",
]
