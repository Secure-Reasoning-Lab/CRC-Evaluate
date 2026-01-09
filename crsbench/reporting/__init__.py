"""Reporting module for CRSBench experiment analysis.

This module provides comprehensive report generation from trial snapshots
after experiment completion.

Key Components:
- ReportGenerator: Main orchestrator for report generation
- SnapshotLoader: Load and parse snapshot archives
- MetricsAggregator: Compute metrics from snapshots
- ExperimentValidator: Validate experiment completeness
- JSONReportGenerator: Generate JSON reports
- HTMLReportGenerator: Generate HTML reports with interactive charts

Example:
    from crsbench.reporting import ReportGenerator
    from pathlib import Path

    generator = ReportGenerator(output_dir=Path("./reports"))

    # Generate reports for an experiment
    result = generator.generate_experiment_report(
        experiment_dir=Path("./experiment_filestore/test-exp"),
    )

    print(f"JSON report: {result['json']}")
    print(f"HTML report: {result['html']}")

For detailed design documentation, see:
    design-docs/reporting/report-generation.md
"""

from crsbench.reporting.errors import (
    ExperimentValidationError,
    MetricsAggregationError,
    ReportError,
    ReportGenerationError,
    SnapshotLoadError,
)
from crsbench.reporting.generators import HTMLReportGenerator, JSONReportGenerator
from crsbench.reporting.metrics import MetricsAggregator
from crsbench.reporting.models import (
    BenchmarkMetrics,
    CRSMetrics,
    ExperimentMetrics,
    ExperimentValidationResult,
    LLMUsageFile,
    ModelUsage,
    SnapshotData,
    SnapshotMetadataFile,
    TimeSeriesPoint,
    TrialInfo,
    TrialMetadataFile,
    TrialMetrics,
    TrialMode,
)
from crsbench.reporting.orchestrator import ReportGenerator
from crsbench.reporting.snapshot_loader import SnapshotLoader, discover_trials
from crsbench.reporting.validator import ExperimentValidator

__all__ = [
    # Main orchestrator
    "ReportGenerator",
    # Core components
    "SnapshotLoader",
    "MetricsAggregator",
    "ExperimentValidator",
    "JSONReportGenerator",
    "HTMLReportGenerator",
    # Data models - File formats
    "TrialMetadataFile",
    "SnapshotMetadataFile",
    "LLMUsageFile",
    "ModelUsage",
    # Data models - Parsed data
    "SnapshotData",
    "TrialInfo",
    # Data models - Metrics
    "TrialMetrics",
    "ExperimentMetrics",
    "TimeSeriesPoint",
    "CRSMetrics",
    "BenchmarkMetrics",
    "ExperimentValidationResult",
    # Enums
    "TrialMode",
    # Utility functions
    "discover_trials",
    # Exceptions
    "ReportError",
    "SnapshotLoadError",
    "MetricsAggregationError",
    "ReportGenerationError",
    "ExperimentValidationError",
]
