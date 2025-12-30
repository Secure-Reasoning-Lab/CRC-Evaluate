"""Benchmark format validation module.

This module provides format validation of benchmark configurations.

For POV verification, use crsbench.evaluation.verification.
For building variants, use crsbench.builder.OSSFuzzBuilder.
"""

# Format validation
from crsbench.builder import BenchmarkMode, VariantType
from crsbench.validation.errors import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)
from crsbench.validation.format_validator import (
    validate_benchmark,
    validate_benchmark_from_string,
    validate_benchmark_suite,
    validate_benchmark_suite_from_string,
    validate_experiment_config,
    validate_experiment_config_from_string,
)
from crsbench.validation.meta_adapter import MetaYamlAdapter

# Re-export verification classes from new location for backward compatibility
from crsbench.evaluation.verification import (
    DeduplicationStrategy,
    OSSFuzzReproducer,
    PatchBasedDedup,
    VerdictResolver,
    VerificationEngine,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
    get_dedup_strategy,
)

__all__ = [
    # Format validation
    "validate_benchmark",
    "validate_benchmark_from_string",
    "validate_experiment_config",
    "validate_experiment_config_from_string",
    "validate_benchmark_suite",
    "validate_benchmark_suite_from_string",
    "ValidationResult",
    "ValidationError",
    "ValidationWarning",
    # Adapter
    "MetaYamlAdapter",
    # Types (from builder)
    "BenchmarkMode",
    "VariantType",
    # Verification (re-exported from evaluation.verification)
    "VerificationStatus",
    "VerificationRequest",
    "VerificationResult",
    "VerificationEngine",
    "VerdictResolver",
    "OSSFuzzReproducer",
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "get_dedup_strategy",
]
