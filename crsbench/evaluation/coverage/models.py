"""Coverage data models for CRSBench.

This module provides Pydantic models for coverage data structures used
during CRS evaluation. Coverage tracking enables measurement of code
coverage achieved by fuzzing corpus files.

Data Formats:
- CorpusCoverage: Per-corpus file coverage data (stored as .cov files)
- CoverageSummary: Aggregated coverage statistics
- CoverageSnapshot: Point-in-time coverage for a snapshot cycle
- CoverageConfig: Configuration for coverage collection
"""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class CoverageConfig(BaseModel):
    """Configuration for coverage collection during CRS evaluation.

    Controls whether coverage is collected and how saturation detection
    is performed. When enabled, coverage data is collected for each
    corpus file during snapshot cycles.

    Attributes:
        enabled: Whether coverage collection is enabled.
        language: Programming language for coverage strategy ('c' or 'java').
        metric: Coverage metric to use ('line', 'edge', or 'function').
        saturation_time: Time in seconds without new coverage before saturation.
            Default is 21600 seconds (6 hours). If no new lines are covered
            for this duration, coverage is considered saturated.
    """

    enabled: bool = False
    language: str = Field(default="c", pattern="^(c|cpp|java|jvm)$")
    metric: str = Field(default="line", pattern="^(line|edge|function)$")
    saturation_time: int = Field(default=21600, ge=0)  # 6 hours in seconds


class FunctionCoverage(BaseModel):
    """Coverage data for a single function.

    Represents the coverage information for one function,
    including its source file location and covered lines.

    Attributes:
        src: Path to the source file containing the function.
        lines: List of line numbers covered within the function.
    """

    src: str
    lines: list[int] = Field(default_factory=list)


class CorpusCoverage(BaseModel):
    """Metadata for a single corpus file.

    Tracks metadata about a fuzzing corpus file, including when it was
    discovered and whether it contributes unique coverage.

    Note: Coverage data is stored separately in corpus_cov/{hash}.cov.json
    files, not in this model. This enables per-input coverage attribution.

    Attributes:
        hash: SHA256 hash of corpus content (12 hex characters).
        first_seen_ts: Unix timestamp when corpus was first observed.
        file_size: Size of the corpus file in bytes.
        contributes_unique: Whether this corpus contributes unique coverage.
    """

    hash: str = Field(..., min_length=12, max_length=12)
    first_seen_ts: float
    file_size: int = Field(..., ge=0)
    contributes_unique: bool = False


class CoverageSummary(BaseModel):
    """Aggregated coverage summary statistics.

    Provides summary statistics for coverage across all corpus files,
    including line and function coverage metrics.

    Attributes:
        metric: Coverage metric type ('line', 'edge', or 'function').
        corpus_total: Total number of corpus files processed.
        corpus_contributing: Number of corpus files that contribute unique coverage
            (add at least one new line not previously covered).
        corpus_unique: Number of corpus files with distinct coverage profiles.
            A corpus has a distinct profile if its exact set of covered lines
            hasn't been seen before. This enables corpus minimization.
        lines_covered: Number of unique lines covered.
        lines_total: Total number of coverable lines.
        lines_percent: Percentage of lines covered (0.0 to 100.0).
        totals_available: Whether total line/function denominators are known.
        functions_covered: Number of functions with at least one covered line.
        functions_total: Total number of functions.
        saturation_detected: Whether coverage saturation has been detected.
    """

    metric: str = Field(default="line", pattern="^(line|edge|function)$")
    corpus_total: int = Field(default=0, ge=0)
    corpus_contributing: int = Field(default=0, ge=0)
    corpus_unique: int = Field(default=0, ge=0)
    lines_covered: int = Field(default=0, ge=0)
    lines_total: int = Field(default=0, ge=0)
    lines_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    totals_available: bool | None = Field(default=None)
    functions_covered: int = Field(default=0, ge=0)
    functions_total: int = Field(default=0, ge=0)
    saturation_detected: bool = False

    @model_validator(mode="after")
    def _infer_totals_available(self) -> "CoverageSummary":
        """Fill totals_available when callers rely on legacy implicit behavior."""
        if self.totals_available is None:
            self.totals_available = self.lines_total > 0 or self.functions_total > 0
        return self

    def format_lines(self) -> str:
        """Format lines coverage for display.

        Returns:
            Formatted string like "10/100 (10.0%)" when total is known,
            or "10 (total N/A)" when total is unknown (e.g., JVM coverage).
        """
        if self.totals_available and self.lines_total > 0:
            return (
                f"{self.lines_covered}/{self.lines_total} ({self.lines_percent:.1f}%)"
            )
        return f"{self.lines_covered} (total N/A)"

    def format_functions(self) -> str:
        """Format functions coverage for display.

        Returns:
            Formatted string like "5/50" when total is known,
            or "5 (total N/A)" when total is unknown.
        """
        if self.totals_available and self.functions_total > 0:
            return f"{self.functions_covered}/{self.functions_total}"
        return f"{self.functions_covered} (total N/A)"


class CoverageSnapshot(BaseModel):
    """Point-in-time coverage data for a snapshot cycle.

    Captures the coverage state at a specific point during CRS execution,
    enabling tracking of coverage progress over time.

    Attributes:
        cycle: Snapshot cycle number (1-indexed).
        timestamp: Unix timestamp when snapshot was captured.
        elapsed_time: Seconds elapsed since trial start.
        harness_name: Name of the harness being fuzzed.
        summary: Aggregated coverage summary for this snapshot.
        saturation_detected: Whether saturation was detected at this cycle.
        new_corpus_count: Number of new corpus files discovered since last cycle.
        new_lines_count: Number of new lines covered since last cycle.
    """

    cycle: int = Field(..., ge=1)
    timestamp: float
    elapsed_time: float = Field(..., ge=0.0)
    harness_name: str
    summary: CoverageSummary
    saturation_detected: bool = False
    new_corpus_count: int = Field(default=0, ge=0)
    new_lines_count: int = Field(default=0, ge=0)

    model_config = {"arbitrary_types_allowed": True}


class CoverageReport(BaseModel):
    """Complete coverage report for a trial.

    Aggregates all coverage snapshots for a trial, providing
    a complete view of coverage progression over time.

    Attributes:
        harness_name: Name of the harness being evaluated.
        snapshots: List of coverage snapshots in chronological order.
        final_summary: Final coverage summary at trial completion.
        saturation_cycle: Cycle number when saturation was detected (None if not).
        success: Whether coverage collection succeeded.
    """

    harness_name: str
    snapshots: list[CoverageSnapshot] = Field(default_factory=list)
    final_summary: Optional[CoverageSummary] = None
    saturation_cycle: Optional[int] = None
    build_time: float = 0.0
    verify_time: float = 0.0
    success: bool = True


class CoveragePovMarker(BaseModel):
    """POV discovery marker to overlay on a coverage timeline."""

    cpv_id: str
    pov_hash: str
    relative_time: float = Field(..., ge=0.0)


class TimedCoverageInput(BaseModel):
    """Normalized timed input for coverage timeline analysis."""

    content_hash: str
    original_name: str
    path: Path
    relative_time: float = Field(..., ge=0.0)
    size: int = Field(..., ge=0)
    lines_covered: int = Field(default=0, ge=0)
    crashed: bool = False
    raw_cov_path: Optional[Path] = None
    crash_log_path: Optional[Path] = None


class CoverageTimelineBucket(BaseModel):
    """Aggregated cumulative line coverage for one time bucket."""

    bucket_start: float = Field(..., ge=0.0)
    bucket_end: float = Field(..., ge=0.0)
    inputs_seen: int = Field(..., ge=0)
    lines_covered: int = Field(..., ge=0)
    lines_total: int = Field(..., ge=0)
    lines_percent: float = Field(..., ge=0.0, le=100.0)


class TrialCoverageContext(BaseModel):
    """Resolved trial context for coverage timeline analysis."""

    trial_dir: Path
    benchmark: str
    harness: str
    seed_dir: Path
    crs_run_start_time: Optional[float] = None
    pov_markers: list[CoveragePovMarker] = Field(default_factory=list)


class CoverageTimelineReport(BaseModel):
    """Coverage-over-time report for one analysis target."""

    benchmark: str
    harness: str
    bucket_size_seconds: int = Field(..., ge=1)
    time_origin: str = Field(
        default="crs_run_start_time",
        description="Reference used for relative_time values in this report.",
    )
    seeds: list[TimedCoverageInput] = Field(default_factory=list)
    pov_markers: list[CoveragePovMarker] = Field(default_factory=list)
    buckets: list[CoverageTimelineBucket] = Field(default_factory=list)
    final_summary: CoverageSummary = Field(default_factory=CoverageSummary)
