"""POV verification module.

This submodule contains POV-specific verification logic:
- VerdictResolver: Determines verification verdict based on crash results
- VerificationEngine: Orchestrates the entire POV verification workflow
"""

from crsbench.evaluation.verification.pov.engine import VerificationEngine
from crsbench.evaluation.verification.pov.verdict import VerdictResolver

__all__ = [
    "VerificationEngine",
    "VerdictResolver",
]
