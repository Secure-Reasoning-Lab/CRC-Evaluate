"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.
"""

from crsbench.evaluation.runner import BenchmarkRunner, EvaluationResult, EvaluationError
from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult, StubCRSExecutor
from crsbench.evaluation.results import ResultCollector, EvaluationReport

__all__ = [
    'BenchmarkRunner',
    'EvaluationResult',
    'EvaluationError',
    'CRSExecutor',
    'CRSResult',
    'StubCRSExecutor',
    'ResultCollector',
    'EvaluationReport'
]