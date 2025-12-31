"""Verification module for POV and patch validation.

This module provides the core verification infrastructure:
- POV Models: PovVerificationStatus, PovVerificationRequest, PovVerificationResult
- Patch Models: PatchVerificationStatus, PatchInfo, PatchVerificationResult, TestMode
- Deduplication strategies: PatchBasedDedup, StatusBasedDedup, NoOpDedup
- POV verification: VerificationEngine, VerdictResolver
- Patch verification: PatchVerificationEngine

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
    # Patch verification models
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    # POV verification models
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
    TestMode,
)
from crsbench.evaluation.verification.patch import PatchVerificationEngine
from crsbench.evaluation.verification.pov import (
    VerdictResolver,
    VerificationEngine,
)

__all__ = [
    # POV verification models
    "PovVerificationStatus",
    "PovVerificationRequest",
    "PovVerificationResult",
    # Patch verification models
    "PatchVerificationStatus",
    "PatchInfo",
    "PatchVerificationResult",
    "TestMode",
    # POV verification engine
    "VerificationEngine",
    "VerdictResolver",
    # Patch verification engine
    "PatchVerificationEngine",
    # Deduplication
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "NoOpDedup",
    "StatusBasedDedup",
    "get_dedup_strategy",
]
