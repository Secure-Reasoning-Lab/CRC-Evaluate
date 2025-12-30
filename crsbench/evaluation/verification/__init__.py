"""Verification module for POV and patch validation.

This module provides the core verification infrastructure:
- Models: VerificationStatus, VerificationRequest, VerificationResult
- Shared components: OSSFuzzReproducer, deduplication strategies
- POV verification: VerificationEngine, VerdictResolver
"""

from crsbench.evaluation.verification.dedup import (
    DeduplicationStrategy,
    NoOpDedup,
    PatchBasedDedup,
    StatusBasedDedup,
    get_dedup_strategy,
)
from crsbench.evaluation.verification.models import (
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)
from crsbench.evaluation.verification.pov import (
    VerdictResolver,
    VerificationEngine,
)
from crsbench.evaluation.verification.reproducer import OSSFuzzReproducer

__all__ = [
    # Models
    "VerificationStatus",
    "VerificationRequest",
    "VerificationResult",
    # Shared components
    "OSSFuzzReproducer",
    # POV verification
    "VerificationEngine",
    "VerdictResolver",
    # Deduplication
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "NoOpDedup",
    "StatusBasedDedup",
    "get_dedup_strategy",
]
