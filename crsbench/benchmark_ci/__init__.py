"""Benchmark CI module for end-to-end testing of CRSBench benchmarks.

This module provides comprehensive testing for benchmarks including:
- Delta/Full mode build verification
- POV reproduction testing (using VerificationEngine)
- Patch verification (using PatchVerificationEngine)
- Coverage measurement (using CoverageEngine)
- Incremental build image pulling

Structure:
- models.py: Data models (JobContext, Task, CIResult, etc.)
- runner.py: BenchmarkCIRunner for orchestrating tests
- jobs/: Job executor classes
- cli/: Command-line interface
"""

from crsbench.benchmark_ci.models import (
    CIResult,
    ExecJobType,
    IncBuildMetrics,
    JobContext,
    JobResult,
    ResultCollector,
    Task,
    TaskMode,
    get_benchmarks_root,
)
from crsbench.benchmark_ci.runner import BenchmarkCIRunner

__all__ = [
    # Runner
    "BenchmarkCIRunner",
    # Models
    "JobContext",
    "ExecJobType",
    "TaskMode",
    "Task",
    "IncBuildMetrics",
    "JobResult",
    "CIResult",
    "ResultCollector",
    # Utilities
    "get_benchmarks_root",
]
