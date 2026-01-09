"""Reporting module exceptions."""


class ReportError(Exception):
    """Base exception for reporting module errors."""


class SnapshotLoadError(ReportError):
    """Error loading or parsing a snapshot archive."""


class MetricsAggregationError(ReportError):
    """Error aggregating metrics from snapshots."""


class ReportGenerationError(ReportError):
    """Error generating a report."""


class ExperimentValidationError(ReportError):
    """Error validating experiment completeness."""
