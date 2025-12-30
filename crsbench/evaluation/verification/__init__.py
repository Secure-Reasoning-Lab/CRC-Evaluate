"""Verification module for POV and patch validation.

This module provides the core verification infrastructure:
- Models: VerificationStatus, VerificationRequest, VerificationResult
- Deduplication strategies: PatchBasedDedup, StatusBasedDedup, NoOpDedup
- POV verification: VerificationEngine, VerdictResolver

Note: Reproduction is handled by OSSFuzzInfrastructure (crsbench.builder).
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

__all__ = [
    # Models
    "VerificationStatus",
    "VerificationRequest",
    "VerificationResult",
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
