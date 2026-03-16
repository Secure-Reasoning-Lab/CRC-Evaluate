"""Helpers for seed coverage timeline analysis."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Optional

from crsbench.evaluation.coverage.models import (
    CoveragePovMarker,
    TimedCoverageInput,
    TrialCoverageContext,
)
from crsbench.validation.schemas import TrialMetadata

if TYPE_CHECKING:
    from pathlib import Path


def discover_trial_seed_dir(trial_dir: Path) -> Optional[Path]:
    """Return the canonical seed directory for a trial.

    Prefers the current contract ``output/seeds`` and falls back to the
    legacy ``output/corpus`` layout.
    """
    seeds_dir = trial_dir / "output" / "seeds"
    if seeds_dir.is_dir():
        return seeds_dir

    legacy_corpus_dir = trial_dir / "output" / "corpus"
    if legacy_corpus_dir.is_dir():
        return legacy_corpus_dir

    return None


def load_trial_context(trial_dir: Path) -> TrialCoverageContext:
    """Load benchmark, harness, seed directory, and POV timing from a trial."""
    metadata_path = trial_dir / "metadata.json"
    seed_dir = discover_trial_seed_dir(trial_dir)
    if seed_dir is None:
        msg = f"No seed directory found under {trial_dir}"
        raise FileNotFoundError(msg)
    metadata = TrialMetadata.model_validate(json.loads(metadata_path.read_text()))
    timeline_duration_seconds = _resolve_trial_duration_seconds(metadata)

    pov_markers: list[CoveragePovMarker] = []
    crs_run_start_time: Optional[float] = None
    pov_store_path = trial_dir / "povs" / "pov_store.json"
    if pov_store_path.exists():
        pov_store = json.loads(pov_store_path.read_text())
        crs_run_start_time = pov_store.get("crs_run_start_time")
        cpv_to_first_pov = pov_store.get("cpv_to_first_pov", {})
        for cpv_id, marker in cpv_to_first_pov.items():
            relative_time = marker.get("relative_time")
            pov_hash = marker.get("pov_hash")
            if relative_time is None or pov_hash is None:
                continue
            pov_markers.append(
                CoveragePovMarker(
                    cpv_id=cpv_id,
                    pov_hash=str(pov_hash),
                    relative_time=float(relative_time),
                )
            )
        pov_markers.sort(key=lambda marker: marker.relative_time)

    return TrialCoverageContext(
        trial_dir=trial_dir,
        benchmark=metadata.benchmark,
        harness=metadata.harness,
        seed_dir=seed_dir,
        crs_run_start_time=crs_run_start_time,
        timeline_duration_seconds=timeline_duration_seconds,
        pov_markers=pov_markers,
    )


def normalize_seed_inputs(
    seed_dir: Path, *, base_time: Optional[float], clamp_negative_to_zero: bool = False
) -> list[TimedCoverageInput]:
    """Normalize raw seed files into a deduplicated timed list.

    Duplicate files are deduplicated by content hash, keeping the earliest
    relative timestamp and original filename.
    """
    visible_files = [
        path
        for path in seed_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ]
    if not visible_files:
        return []

    if base_time is None:
        base_time = min(path.stat().st_mtime for path in visible_files)

    normalized_by_hash: dict[str, TimedCoverageInput] = {}
    for seed_path in visible_files:
        file_mtime = seed_path.stat().st_mtime
        relative_time = float(file_mtime - base_time)
        if relative_time < 0:
            if not clamp_negative_to_zero:
                continue
            relative_time = 0.0
        content_hash = _hash_file(seed_path)
        candidate = TimedCoverageInput(
            content_hash=content_hash,
            original_name=seed_path.name,
            path=seed_path,
            relative_time=relative_time,
            size=seed_path.stat().st_size,
        )
        existing = normalized_by_hash.get(content_hash)
        if existing is None or candidate.relative_time < existing.relative_time:
            normalized_by_hash[content_hash] = candidate

    return sorted(
        normalized_by_hash.values(),
        key=lambda item: (item.relative_time, item.original_name, item.content_hash),
    )


def _hash_file(path: Path) -> str:
    """Compute the stable content hash used for seed deduplication."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def _resolve_trial_duration_seconds(metadata: TrialMetadata) -> Optional[float]:
    """Return the authoritative timeline duration for a trial, if present."""
    if metadata.run_time is not None:
        return float(metadata.run_time)
    return None
