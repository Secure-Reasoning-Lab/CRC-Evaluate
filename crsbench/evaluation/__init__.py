"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.
"""

from crsbench.evaluation.analysis import (
    AnalysisManager,
    AnalysisResult,
    AnalyzerInterface,
)
from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor
from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult, StubCRSExecutor
from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.results import EvaluationReport, ResultCollector
from crsbench.evaluation.runner import (
    BenchmarkRunner,
    EvaluationError,
    EvaluationResult,
)
from crsbench.evaluation.snapshot import (
    SnapshotMetadata,
    SnapshotSummary,
    extract_snapshot,
    get_completion_marker_path,
    get_snapshot_archive_path,
    inspect_snapshot,
    is_snapshot_complete,
    list_snapshots,
    load_snapshot_metadata,
    validate_snapshot_structure,
)
from crsbench.evaluation.snapshot_manager import SnapshotManager

__all__ = [
    "BenchmarkRunner",
    "EvaluationResult",
    "EvaluationError",
    "CRSExecutor",
    "CRSResult",
    "StubCRSExecutor",
    "CRSBugFindingExecutor",
    "CRSPatchExecutor",
    "ResultCollector",
    "EvaluationReport",
    "SnapshotMetadata",
    "SnapshotSummary",
    "SnapshotManager",
    "is_snapshot_complete",
    "get_snapshot_archive_path",
    "get_completion_marker_path",
    "list_snapshots",
    "load_snapshot_metadata",
    "inspect_snapshot",
    "extract_snapshot",
    "validate_snapshot_structure",
    "AnalyzerInterface",
    "AnalysisResult",
    "AnalysisManager",
]
