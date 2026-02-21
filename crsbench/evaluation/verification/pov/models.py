"""Data models for POV verification.

This module provides Pydantic models for POV verification data:
- POVEntry: Metadata for a single POV file
- POVSnapshot: Snapshot of verification state at a point in time
- POVVerificationReport: Final report of POV verification during evaluation

Note: Uses PovVerificationStatus from verification/models.py (no duplicate enum).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from crsbench.evaluation.verification.models import PovVerificationStatus


class POVEntry(BaseModel):
    """Metadata for a single POV file."""

    hash: str = Field(description="SHA256 hash (first 16 hex chars) of POV content")
    first_seen_ts: float = Field(description="Timestamp when POV was first discovered")
    file_mtime: Optional[float] = Field(
        default=None,
        description="File modification time (mtime) from CRS - actual creation time",
    )
    file_size: int = Field(description="Size of POV file in bytes")
    status: PovVerificationStatus = Field(description="Verification outcome")
    cpv_matched: list[str] = Field(
        default_factory=list,
        description="CPV identifiers matched (e.g., ['cpv_0', 'cpv_1'])",
    )
    crash_log_path: Optional[str] = Field(
        default=None, description="Relative path to crash log file"
    )
    crash_signature: Optional[str] = Field(
        default=None,
        description="Crash signature hash from sanitizer stack trace (16-char hex)",
    )
    verification_duration: float = Field(
        default=0.0, description="Time taken to verify (seconds)"
    )


class POVSnapshot(BaseModel):
    """Snapshot of POV verification state at a point in time."""

    cycle: int = Field(description="Snapshot cycle number")
    timestamp: float = Field(description="Unix timestamp")
    elapsed_time: float = Field(description="Seconds since trial start")
    harness_name: str = Field(description="Harness being evaluated")
    cpvs_found: list[str] = Field(
        default_factory=list, description="CPVs discovered so far"
    )
    cpvs_remaining: list[str] = Field(
        default_factory=list, description="CPVs not yet discovered"
    )
    povs_total: int = Field(default=0, description="Total unique POVs processed")
    povs_new: int = Field(default=0, description="New POVs in this cycle")
    duplicates_skipped: int = Field(
        default=0, description="Duplicates skipped in this cycle"
    )
    unintended_crashes_count: int = Field(
        default=0, description="Unintended crashes found so far"
    )
    early_stop_triggered: bool = Field(
        default=False, description="Whether early stop was triggered"
    )


class POVVerificationReport(BaseModel):
    """Final report of POV verification during evaluation."""

    benchmark_id: str = Field(description="Benchmark identifier")
    harness_name: str = Field(description="Harness name")
    total_expected_cpvs: int = Field(description="Total CPVs in ground truth")
    cpvs_found: list[str] = Field(
        default_factory=list, description="CPV identifiers discovered"
    )
    cpvs_remaining: list[str] = Field(
        default_factory=list, description="CPV identifiers not discovered"
    )
    total_povs_processed: int = Field(
        default=0, description="Total unique POVs verified"
    )
    duplicates_skipped: int = Field(
        default=0, description="Duplicate POVs not re-verified"
    )
    unintended_crashes: int = Field(default=0, description="Unintended crashes found")
    unique_unintended_crash_sites: int = Field(
        default=0,
        description="Number of unique crash sites among unintended crashes",
    )
    verification_errors: int = Field(default=0, description="Verification failures")
    verification_timeouts: int = Field(default=0, description="Verification timeouts")
    early_stopped: bool = Field(
        default=False, description="Whether early termination triggered"
    )
    early_stop_time: Optional[datetime] = Field(
        default=None, description="When early stop was triggered"
    )
    total_duration_seconds: float = Field(
        default=0.0, description="Total monitoring duration"
    )

    def to_dict(self) -> dict:
        """Convert report to dictionary for JSON serialization.

        Uses Pydantic's model_dump with JSON mode for proper datetime handling.
        """
        return self.model_dump(mode="json")
