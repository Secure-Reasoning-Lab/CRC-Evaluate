"""Benchmark CI module for validating CRSBench benchmarks.

This module provides two approaches to benchmark validation:

1. **BenchmarkValidator** (existing): High-level orchestration using existing engines
   - VerificationEngine: POV verification (builds all variants, runs POVs)
   - PatchVerificationEngine: Patch verification
   - CoverageEngine: Coverage collection

2. **Job-based approach** (new): Fine-grained job tracking with two-phase execution
   - JobFactory: Creates build and verify jobs from benchmark config
   - ProjectCIRunner: Executes jobs in build-then-verify phases
   - Explicit job tracking with timing, logs, and artifacts

Usage (BenchmarkValidator):
    from crsbench.benchmark_ci import BenchmarkValidator

    validator = BenchmarkValidator()
    result = validator.validate_benchmark(benchmark_path)

Usage (Job-based):
    from crsbench.benchmark_ci import JobFactory, ProjectCIRunner
    from crsbench.validation import MetaYamlAdapter

    adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
    factory = JobFactory(adapter, sanitizer="address")
    jobs = factory.create_all_jobs()

    runner = ProjectCIRunner(oss_fuzz_path)
    result = runner.run(jobs)

CLI:
    crsbench ci --all
    crsbench ci --benchmarks bench1,bench2
"""

from crsbench.benchmark_ci.checks import (
    check_coverage,
    check_patch_verify,
    check_verify,
    get_expected_cpvs,
)
from crsbench.benchmark_ci.factory import JobFactory, create_jobs_for_benchmark
from crsbench.benchmark_ci.jobs import (
    BuildJob,
    Job,
    JobContext,
    JobResult,
    VerifyPatchJob,
    VerifyPovJob,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.runner import ProjectCIResult, ProjectCIRunner
from crsbench.benchmark_ci.validator import BenchmarkValidator

__all__ = [
    # Main validator (existing)
    "BenchmarkValidator",
    # Job-based components (new)
    "Job",
    "JobContext",
    "JobResult",
    "BuildJob",
    "VerifyPovJob",
    "VerifyPatchJob",
    "JobFactory",
    "create_jobs_for_benchmark",
    "ProjectCIRunner",
    "ProjectCIResult",
    # Models
    "CheckResult",
    "CheckStatus",
    "BenchmarkValidationResult",
    "ValidationSummary",
    # Result checking functions
    "check_verify",
    "check_patch_verify",
    "check_coverage",
    "get_expected_cpvs",
]
