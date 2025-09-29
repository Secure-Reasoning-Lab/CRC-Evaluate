"""POV deduplication module for CRSBench.

This module provides functionality to deduplicate Proof of Vulnerabilities (POVs)
based on root cause analysis, as specified in the CRSBench RFC.
"""

from crsbench.deduplication.analyzer import RootCauseAnalyzer, RootCause
from crsbench.deduplication.deduplicator import POVDeduplicator, DeduplicationResult
from crsbench.deduplication.strategies import DeduplicationStrategy, LocationBasedStrategy, StackTraceStrategy
from crsbench.deduplication.integration import deduplicate_evaluation_results

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