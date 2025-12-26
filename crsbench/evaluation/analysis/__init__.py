"""CRS-specific analysis module.

This module provides a pluggable interface for CRS developers to analyze
their custom crs-data/ outputs from trials and snapshots.

Main classes:
- AnalyzerInterface: Abstract base class for implementing analyzers
- AnalysisResult: Data structure for analyzer results
- AnalysisManager: Auto-discovers and executes analyzers

Example usage:
    >>> from crsbench.evaluation.analysis import AnalysisManager
    >>> manager = AnalysisManager()
    >>> result = manager.analyze_snapshot("atlantis-c", Path("/tmp/snapshot-0001"))
    >>> if result:
    >>>     print(result.summary)
    >>>     print(result.metrics)
"""

from crsbench.evaluation.analysis.base import AnalysisResult, AnalyzerInterface
from crsbench.evaluation.analysis.manager import AnalysisManager

__all__ = [
    "AnalyzerInterface",
    "AnalysisResult",
    "AnalysisManager",
]
