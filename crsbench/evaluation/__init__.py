"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.
"""

from .runner import BenchmarkRunner, EvaluationResult
from .crs_executor import CRSExecutor, CRSResult, StubCRSExecutor
from .results import ResultCollector, EvaluationReport

__all__ = [
    'BenchmarkRunner',
    'EvaluationResult',
    'CRSExecutor',
    'CRSResult',
    'StubCRSExecutor',
    'ResultCollector',
    'EvaluationReport'
]