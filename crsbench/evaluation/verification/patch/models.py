"""Data models for patch verification manager.

This module provides Pydantic models for patch verification tracking:
- PatchSnapshot: Snapshot of patch discovery state at a point in time
- PatchVerificationReport: Final report of patch discovery during evaluation

Note: PatchVerificationResult and related models are in verification/models.py.
This module is specifically for the PatchVerificationManager's tracking models.
"""

from typing import Optional

from pydantic import BaseModel, Field


class PatchSnapshot(BaseModel):
    """Snapshot of patch discovery state at a point in time.

    Used by PatchVerificationManager to track patch generation progress
    during bug-fixing CRS evaluation.

    Note: Patches are organized by CPV ID (e.g., cpv_0, cpv_1). Each CPV may have
    one or more POVs that trigger it, but patches are generated per-CPV.
    """

    cycle: int = Field(description="Snapshot cycle number")
    timestamp: float = Field(description="Unix timestamp")
    elapsed_time: float = Field(description="Seconds since trial start")
    harness_name: str = Field(description="Harness being evaluated")
    cpvs_with_patches: list[str] = Field(
        default_factory=list,
        description="CPV IDs with patches discovered (e.g., ['cpv_0', 'cpv_1'])",
    )
    patches_total: int = Field(default=0, description="Total patches discovered")
    patches_new: int = Field(default=0, description="New patches in this cycle")
    input_cpvs_total: int = Field(
        default=0, description="Total input CPVs to generate patches for"
    )


class PatchDiscoveryReport(BaseModel):
    """Final report of patch discovery during evaluation.

    Note: This tracks patch discovery only. Full patch verification
    (build, POV test, unit test) is done post-evaluation by PatchVerificationEngine.
    """

    benchmark_id: str = Field(description="Benchmark identifier")
    harness_name: str = Field(description="Harness name")
    input_cpvs_total: int = Field(
        description="Total input CPVs to generate patches for"
    )
    cpvs_with_patches: list[str] = Field(
        default_factory=list,
        description="CPV IDs with patches discovered",
    )
    patches_total: int = Field(default=0, description="Total patches discovered")
    total_duration_seconds: float = Field(
        default=0.0, description="Total monitoring duration"
    )
    snapshot_count: int = Field(default=0, description="Number of snapshots captured")
    latest_snapshot: Optional[PatchSnapshot] = Field(
        default=None, description="Most recent snapshot"
    )

    def to_dict(self) -> dict:
        """Convert report to dictionary for JSON serialization.

        Uses Pydantic's model_dump with JSON mode for proper handling.
        """
        return self.model_dump(mode="json")
