"""Verification module for POV validation."""

from crsbench.validation.verification.models import (
    VerificationStatus,
    VerificationRequest,
    VerificationResult,
)
from crsbench.validation.verification.reproducer import OSSFuzzReproducer
from crsbench.validation.verification.verdict import VerdictResolver
from crsbench.validation.verification.dedup import (
    DeduplicationStrategy,
    PatchBasedDedup,
    NoOpDedup,
    StatusBasedDedup,
    get_dedup_strategy,
)
from crsbench.validation.verification.engine import VerificationEngine

__all__ = [
    # Models
    'VerificationStatus',
    'VerificationRequest',
    'VerificationResult',
    # Components
    'OSSFuzzReproducer',
    'VerdictResolver',
    'VerificationEngine',
    # Deduplication
    'DeduplicationStrategy',
    'PatchBasedDedup',
    'NoOpDedup',
    'StatusBasedDedup',
    'get_dedup_strategy',
]
