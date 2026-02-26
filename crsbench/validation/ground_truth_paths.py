"""Canonical benchmark ground-truth path helpers.

Centralizes benchmark/.aixcc path construction to avoid drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def validate_ground_truth_segment(segment: str, *, label: str) -> str:
    """Validate a single benchmark layout path segment.

    Segment values are names (harness / cpv id), not paths.
    """
    if not isinstance(segment, str):
        raise TypeError(f"{label} must be a string")

    normalized = segment.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    if normalized in {".", ".."}:
        raise ValueError(f"{label} cannot be '.' or '..'")
    if "/" in normalized or "\\" in normalized:
        raise ValueError(f"{label} cannot contain path separators")
    if PurePath(normalized).is_absolute():
        raise ValueError(f"{label} cannot be an absolute path")
    return normalized


@dataclass(frozen=True)
class GroundTruthPaths:
    """Path resolver for benchmark `.aixcc` layout."""

    benchmark_dir: Path

    @property
    def aixcc_dir(self) -> Path:
        return self.benchmark_dir / ".aixcc"

    @property
    def meta_yaml(self) -> Path:
        return self.aixcc_dir / "meta.yaml"

    @property
    def ref_diff(self) -> Path:
        return self.aixcc_dir / "ref.diff"

    def harness_dir(self, harness: str) -> Path:
        harness_name = validate_ground_truth_segment(harness, label="harness")
        return self.aixcc_dir / harness_name

    def cpv_dir(self, harness: str, cpv_id: str) -> Path:
        cpv_name = validate_ground_truth_segment(cpv_id, label="cpv_id")
        return self.harness_dir(harness) / cpv_name

    def cpv_blobs_dir(self, harness: str, cpv_id: str) -> Path:
        return self.cpv_dir(harness, cpv_id) / "blobs"

    def cpv_hints_dir(self, harness: str, cpv_id: str) -> Path:
        return self.cpv_dir(harness, cpv_id) / "hints"

    def harness_seeds_dir(self, harness: str) -> Path:
        return self.harness_dir(harness) / "seeds"

    def harness_corpus_dir(self, harness: str) -> Path:
        return self.harness_dir(harness) / "corpus"


# Keep serialized module identity backward compatible with historical imports.
GroundTruthPaths.__module__ = "crsbench.benchmark.paths"
