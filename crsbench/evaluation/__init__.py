"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.
"""

from crsbench.evaluation.runner import BenchmarkRunner, EvaluationResult, EvaluationError
from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult, StubCRSExecutor
from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
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
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.evaluation.analysis import (
    AnalyzerInterface,
    AnalysisResult,
    AnalysisManager
)

__all__ = [
    'BenchmarkRunner',
    'EvaluationResult',
    'EvaluationError',
    'CRSExecutor',
    'CRSResult',
    'StubCRSExecutor',
    'CRSBugFindingExecutor',
    'CRSPatchExecutor',
    'ResultCollector',
    'EvaluationReport',
    'SnapshotMetadata',
    'SnapshotSummary',
    'SnapshotManager',
    'is_snapshot_complete',
    'get_snapshot_archive_path',
    'get_completion_marker_path',
    'list_snapshots',
    'load_snapshot_metadata',
    'inspect_snapshot',
    'extract_snapshot',
    'validate_snapshot_structure',
    'AnalyzerInterface',
    'AnalysisResult',
    'AnalysisManager'
]