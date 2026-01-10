"""Patch verification module.

This module provides:
- PatchVerificationEngine: For validating CRS-generated patches (post-evaluation)
- PatchVerificationManager: For real-time patch tracking during CRS execution
"""

from crsbench.evaluation.verification.patch.engine import PatchVerificationEngine
from crsbench.evaluation.verification.patch.manager import PatchVerificationManager
from crsbench.evaluation.verification.patch.models import (
    PatchDiscoveryReport,
    PatchSnapshot,
)

__all__ = [
    "PatchVerificationEngine",
    "PatchVerificationManager",
    "PatchSnapshot",
    "PatchDiscoveryReport",
]
