"""POV verification module.

This submodule contains POV-specific verification logic:
- VerdictResolver: Determines verification verdict based on crash results
- VerificationEngine: Orchestrates the entire POV verification workflow
- POVVerificationConfig: Configuration for POV verification
- POVEntry: Metadata for a single POV file
- POVSnapshot: Snapshot of verification state at a point in time
- POVVerificationReport: Final report of POV verification
- POVStore: In-memory POV store with JSON persistence
- POVVerificationManager: Real-time POV verification during CRS evaluation

Note: Uses PovVerificationStatus from verification/models.py (no duplicate enum).
State tracking follows CoverageManager pattern (no separate state class).
"""

from crsbench.evaluation.verification.pov.config import POVVerificationConfig
from crsbench.evaluation.verification.pov.engine import VerificationEngine
from crsbench.evaluation.verification.pov.manager import POVVerificationManager
from crsbench.evaluation.verification.pov.models import (
    POVEntry,
    POVSnapshot,
    POVVerificationReport,
)
from crsbench.evaluation.verification.pov.store import POVStore
from crsbench.evaluation.verification.pov.verdict import VerdictResolver

__all__ = [
    "POVEntry",
    "POVSnapshot",
    "POVStore",
    "POVVerificationConfig",
    "POVVerificationManager",
    "POVVerificationReport",
    "VerificationEngine",
    "VerdictResolver",
]
