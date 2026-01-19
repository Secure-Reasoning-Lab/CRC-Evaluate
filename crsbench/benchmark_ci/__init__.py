"""Simplified benchmark CI module for validating CRSBench benchmarks.

This module provides benchmark validation using existing verification engines:
- VerificationEngine: POV verification (builds all variants, runs POVs)
- PatchVerificationEngine: Patch verification
- CoverageEngine: Coverage collection

No custom build logic - delegates everything to existing, tested code.

Usage:
    from crsbench.benchmark_ci import BenchmarkValidator

    validator = BenchmarkValidator()
    result = validator.validate_benchmark(benchmark_path)

Result checking:
    from crsbench.benchmark_ci import check_verify, check_patch_verify, check_coverage

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
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import BenchmarkValidator

__all__ = [
    # Main validator
    "BenchmarkValidator",
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
