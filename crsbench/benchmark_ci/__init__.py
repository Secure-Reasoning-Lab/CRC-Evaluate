"""Benchmark CI module for validating CRSBench benchmarks.

This module provides benchmark validation using Redis-based distributed execution:

**Job-based architecture:**
- BuildSingleVariantJob: Build a single variant for a benchmark
- VerifyCpvPovJob: Verify POVs for a single CPV
- BuildPatchVariantJob: Build a patched variant
- PatchVariantTestJob: Run POVs + tests on a patched build
- FlatCollectCoverageJob: Collect coverage for a benchmark

Jobs are executed via Redis queues processed by evaluator workers.

CLI:
    crsbench ci --all
    crsbench ci --benchmarks bench1 bench2
"""

from crsbench.benchmark_ci.checks import (
    check_coverage,
    check_patch_verify,
    check_verify,
    get_expected_cpvs,
)
from crsbench.benchmark_ci.jobs import (
    BuildPatchVariantJob,
    FlatCollectCoverageJob,
    Job,
    JobContext,
    JobResult,
    PatchVariantTestJob,
    VerifyCpvPovJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import BenchmarkValidator

__all__ = [
    "BenchmarkValidationResult",
    "BenchmarkValidator",
    "BuildPatchVariantJob",
    "CheckResult",
    "CheckStatus",
    "FlatCollectCoverageJob",
    "Job",
    "JobContext",
    "JobResult",
    "PatchVariantTestJob",
    "ValidationSummary",
    "VerifyCpvPovJob",
    "check_coverage",
    "check_patch_verify",
    "check_verify",
    "get_expected_cpvs",
]
