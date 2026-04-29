"""Pydantic models for the reporting module.

Shared models (used across evaluation and reporting):
- Imported from crsbench.schemas

Reporting-specific models:
- SnapshotData: Parsed and aggregated snapshot data
- TrialInfo: Discovered trial information
- TrialMetrics, CRSMetrics, BenchmarkMetrics, ExperimentMetrics: Metrics models
- ExperimentValidationResult: Validation results
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Import shared schemas from validation module
from crsbench.validation.schemas import (
    HintsStats,
    LLMUsage,
    ModelUsage,
    PovsStats,
    SnapshotMetadata,
    SourceInfo,
    TrialConfig,
    TrialMetadata,
    TrialMode,
)

# Aliases for backward compatibility (old names used *File suffix)
TrialMetadataFile = TrialMetadata
SnapshotMetadataFile = SnapshotMetadata
LLMUsageFile = LLMUsage

# Re-export shared schemas for backward compatibility
__all__ = [
    # Shared schemas (re-exported)
    "TrialMode",
    "SourceInfo",
    "HintsStats",
    "PovsStats",
    "TrialConfig",
    "TrialMetadata",
    "TrialMetadataFile",
    "SnapshotMetadata",
    "SnapshotMetadataFile",
    "ModelUsage",
    "LLMUsage",
    "LLMUsageFile",
    # Reporting-specific models
    "SnapshotData",
    "TrialInfo",
    "TimeSeriesPoint",
    "TrialMetrics",
    "CRSMetrics",
    "BenchmarkMetrics",
    "ExperimentMetrics",
    "ExperimentValidationResult",
]


# =============================================================================
# Parsed Snapshot Data (aggregated from extracted archive)
# =============================================================================


class SnapshotData(BaseModel):
    """Parsed and aggregated snapshot data.

    This is created by SnapshotLoader after extracting and parsing
    a snapshot archive.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # From trial context
    trial_dir: Path

    # From snapshot metadata.json
    cycle: int
    timestamp: float
    elapsed_time: float
    snapshot_period: int
    running_elapsed_time: float | None = None

    # Parsed content counts
    pov_count: int = 0
    patch_count: int = 0
    corpus_count: int = 0

    # POV file names (for deduplication tracking)
    pov_names: list[str] = Field(default_factory=list)
    patch_names: list[str] = Field(default_factory=list)

    # LLM usage (parsed from llm-usage.json)
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)

    # POV verification data (from pov_verification.json)
    cpvs_found: list[str] = Field(default_factory=list)
    cpvs_remaining: list[str] = Field(default_factory=list)
    early_stop_triggered: bool = False

    # Flags indicating what was captured
    has_config: bool = False
    has_execution_metadata: bool = False
    has_crs_log: bool = False
    has_coverage: bool = False


# =============================================================================
# Trial Info (discovered trial information)
# =============================================================================


class TrialInfo(BaseModel):
    """Information about a discovered trial directory.

    This is created during trial discovery by scanning experiment directories.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Directory info
    trial_dir: Path
    trial_num: int

    # From metadata.json (if available)
    crs: str = "unknown"
    benchmark: str = "unknown"
    harness: str = "unknown"
    mode: TrialMode = TrialMode.unknown

    # Validation status
    status: str = "valid"  # "valid", "missing_metadata", "invalid_metadata"
    error: str | None = None

    # Execution readiness markers (shared by reporting/re-eval flows)
    has_success_marker: bool = False
    has_fail_marker: bool = False
    execution_status: str = "incomplete"  # "success", "failed", "incomplete"
    reeval_ready: bool = False
    reeval_reason: str | None = None

    # Full metadata (if loaded)
    metadata: TrialMetadata | None = None

    # Snapshot info
    snapshot_count: int = 0
    complete_snapshot_count: int = 0


# =============================================================================
# Metrics Models
# =============================================================================


class TimeSeriesPoint(BaseModel):
    """Single point in time series data."""

    elapsed_time: float
    running_elapsed_time: float = 0.0
    cumulative_povs: int = 0
    cumulative_patches: int = 0
    llm_tokens: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost: float = 0.0


class TrialMetrics(BaseModel):
    """Aggregated metrics for a single trial."""

    model_config = ConfigDict(extra="allow")

    # Identifiers
    trial_dir: str
    trial_num: int
    crs: str
    benchmark: str
    harness: str
    mode: TrialMode

    # Run configuration (extracted from trial path)
    run_mode: str | None = None  # "full" or "delta"
    sanitizer: str | None = None  # "address", "undefined", etc.

    # POV metrics
    total_povs_discovered: int = 0
    unique_pov_names: list[str] = Field(default_factory=list)

    # POV verification status breakdown (from <trial>/povs/pov_store.json)
    povs_cpv: int = 0
    povs_unintended: int = 0
    povs_not_vulnerable: int = 0
    povs_error: int = 0
    unintended_unique_sites: int = 0

    # Patch metrics
    total_patches_generated: int = 0
    unique_patch_names: list[str] = Field(default_factory=list)

    # Cost metrics
    total_llm_cost: float = 0.0
    total_llm_tokens: int = 0
    total_llm_input_tokens: int = 0
    total_llm_output_tokens: int = 0
    llm_usage_by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Time metrics
    total_time: float = 0.0
    time_to_first_pov: float | None = None

    # Provenance for the trial-start epoch used when computing
    # ``time_to_first_pov`` and CPV trigger times. See
    # ``crsbench.reporting.metrics.resolve_trial_anchor`` for the source
    # labels and their relative precision.
    anchor_source: str | None = None

    # Time-series data
    time_series: list[TimeSeriesPoint] = Field(default_factory=list)

    # Snapshot info
    snapshot_count: int = 0

    # Early stop analysis
    total_cpvs: int = 0
    cpvs_found_count: int = 0
    all_cpvs_found: bool = False
    early_stop_time: float | None = None
    early_stop_cost: float | None = None
    time_saved: float | None = None
    cost_saved: float | None = None

    @property
    def unique_povs(self) -> int:
        """Number of unique POVs discovered."""
        return len(self.unique_pov_names)

    @property
    def unique_patches(self) -> int:
        """Number of unique patches generated."""
        return len(self.unique_patch_names)


class CRSMetrics(BaseModel):
    """Aggregated metrics for a CRS across all trials."""

    crs: str
    trial_count: int = 0
    avg_povs: float = 0.0
    avg_patches: float = 0.0
    avg_cost: float = 0.0
    total_cost: float = 0.0
    total_povs: int = 0


class BenchmarkMetrics(BaseModel):
    """Aggregated metrics for a benchmark across all trials."""

    benchmark: str
    trial_count: int = 0
    avg_povs: float = 0.0
    avg_patches: float = 0.0
    avg_time_to_first_pov: float | None = None
    total_cost: float = 0.0


class ExperimentMetrics(BaseModel):
    """Aggregated metrics across entire experiment."""

    model_config = ConfigDict(extra="allow")

    experiment_dir: str
    total_trials: int = 0
    valid_trials: int = 0

    # Averages
    avg_povs_per_trial: float = 0.0
    avg_patches_per_trial: float = 0.0
    avg_cost_per_trial: float = 0.0

    # Grouped metrics
    by_crs: dict[str, CRSMetrics] = Field(default_factory=dict)
    by_benchmark: dict[str, BenchmarkMetrics] = Field(default_factory=dict)

    # Individual trial metrics
    trial_metrics: list[TrialMetrics] = Field(default_factory=list)


# =============================================================================
# Validation Result
# =============================================================================


class ExperimentValidationResult(BaseModel):
    """Result from experiment completeness validation."""

    experiment_dir: str
    total_discovered: int
    valid_count: int
    missing_metadata_count: int
    invalid_metadata_count: int

    valid_trials: list[TrialInfo] = Field(default_factory=list)
    invalid_trials: list[TrialInfo] = Field(default_factory=list)

    def is_complete(self) -> bool:
        """Check if all discovered trials are valid."""
        return self.missing_metadata_count == 0 and self.invalid_metadata_count == 0
