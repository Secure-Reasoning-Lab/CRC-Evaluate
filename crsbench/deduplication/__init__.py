"""POV deduplication module for CRSBench.

This module provides functionality to deduplicate Proof of Vulnerabilities (POVs)
based on root cause analysis, as specified in the CRSBench RFC.
"""

from .analyzer import RootCauseAnalyzer, RootCause
from .deduplicator import POVDeduplicator, DeduplicationResult
from .strategies import DeduplicationStrategy, LocationBasedStrategy, StackTraceStrategy
from .integration import deduplicate_evaluation_results

__all__ = [
    'RootCauseAnalyzer',
    'RootCause',
    'POVDeduplicator',
    'DeduplicationResult',
    'DeduplicationStrategy',
    'LocationBasedStrategy',
    'StackTraceStrategy',
    'deduplicate_evaluation_results'
]