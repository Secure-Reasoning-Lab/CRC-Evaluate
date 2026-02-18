"""Benchmark evaluation module for CRSBench.

This module provides functionality to execute benchmarks against CRS systems,
collect results, and report on POV detection performance.

Submodules:
- verification: POV and patch verification
- coverage: Coverage collection and analysis
"""

from crsbench.evaluation.analysis import (
    AnalysisManager,
    AnalysisResult,
    AnalyzerInterface,
)
from crsbench.evaluation.results import (
    CRSExecutionResult,
    EvaluationReport,
    ResultCollector,
)
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
from crsbench.evaluation.verification import (
    DeduplicationStrategy,
    PatchBasedDedup,
    PatchVerificationEngine,
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
    VerdictResolver,
    VerificationEngine,
    get_dedup_strategy,
)

__all__ = [
    # Runner
    "BenchmarkRunner",
    "EvaluationResult",
    "EvaluationError",
    # Results
    "CRSExecutionResult",
    "ResultCollector",
    "EvaluationReport",
    # Snapshots
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
    # Analysis
    "AnalyzerInterface",
    "AnalysisResult",
    "AnalysisManager",
    # POV Verification
    "PovVerificationStatus",
    "PovVerificationRequest",
    "PovVerificationResult",
    "VerificationEngine",
    "VerdictResolver",
    "DeduplicationStrategy",
    "PatchBasedDedup",
    "get_dedup_strategy",
    # Patch Verification
    "PatchVerificationEngine",
]
