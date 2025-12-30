"""Benchmark format validation and POV verification module.

This module provides:
1. Format validation of benchmark configurations
2. POV (Proof of Vulnerability) verification against benchmark variants

For building variants, use crsbench.builder.OSSFuzzBuilder.
"""

# Format validation
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

# POV verification
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.variant import (
    BenchmarkMode,
    BuildVersion,
    VariantType,
)
from crsbench.validation.verification import (
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
    # POV verification - Adapter
    "MetaYamlAdapter",
    # POV verification - Variant
    "BenchmarkMode",
    "BuildVersion",
    "VariantType",
    # POV verification - Verification
    "VerificationStatus",
    "VerificationRequest",
    "VerificationResult",
    "VerificationEngine",
    "VerdictResolver",
    "OSSFuzzReproducer",
    # POV verification - Deduplication
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "get_dedup_strategy",
]
