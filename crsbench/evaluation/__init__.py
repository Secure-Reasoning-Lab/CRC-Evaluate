"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.
"""

from crsbench.evaluation.runner import BenchmarkRunner, EvaluationResult, EvaluationError
from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult, StubCRSExecutor
from crsbench.evaluation.results import ResultCollector, EvaluationReport
from crsbench.evaluation.snapshot import (
    SnapshotMetadata,
    SnapshotSummary,
    is_snapshot_complete,
    get_snapshot_archive_path,
    get_completion_marker_path,
    list_snapshots,
    load_snapshot_metadata,
    inspect_snapshot,
    extract_snapshot,
    validate_snapshot_structure
)

__all__ = [
    'BenchmarkRunner',
    'EvaluationResult',
    'EvaluationError',
    'CRSExecutor',
    'CRSResult',
    'StubCRSExecutor',
    'ResultCollector',
    'EvaluationReport',
    'SnapshotMetadata',
    'SnapshotSummary',
    'is_snapshot_complete',
    'get_snapshot_archive_path',
    'get_completion_marker_path',
    'list_snapshots',
    'load_snapshot_metadata',
    'inspect_snapshot',
    'extract_snapshot',
    'validate_snapshot_structure'
]