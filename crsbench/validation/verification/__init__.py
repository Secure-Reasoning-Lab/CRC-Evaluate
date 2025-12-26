"""Verification module for POV validation."""

from crsbench.validation.verification.dedup import (
    DeduplicationStrategy,
    NoOpDedup,
    PatchBasedDedup,
    StatusBasedDedup,
    get_dedup_strategy,
)
from crsbench.validation.verification.engine import VerificationEngine
from crsbench.validation.verification.models import (
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)
from crsbench.validation.verification.reproducer import OSSFuzzReproducer
from crsbench.validation.verification.verdict import VerdictResolver

__all__ = [
    # Models
    "VerificationStatus",
    "VerificationRequest",
    "VerificationResult",
    # Components
    "OSSFuzzReproducer",
    "VerdictResolver",
    "VerificationEngine",
    # Deduplication
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "NoOpDedup",
    "StatusBasedDedup",
    "get_dedup_strategy",
]
