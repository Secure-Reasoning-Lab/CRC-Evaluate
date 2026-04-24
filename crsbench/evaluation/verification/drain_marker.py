"""Shared marker helpers for incomplete async verification drains."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Literal

VERIFICATION_UNDRAINED_MARKER_FILENAME = ".verification-undrained.json"
VerificationKind = Literal["patch", "pov"]


def verification_undrained_marker_path(trial_dir: Path) -> Path:
    """Return the shared async verification drain marker path."""
    return trial_dir / VERIFICATION_UNDRAINED_MARKER_FILENAME


def clear_verification_undrained_marker(trial_dir: Path) -> None:
    """Remove the shared marker if it exists."""
    verification_undrained_marker_path(trial_dir).unlink(missing_ok=True)


def read_verification_undrained_marker(trial_dir: Path) -> dict[str, object] | None:
    """Read the shared marker payload when present and well-formed."""
    marker_path = verification_undrained_marker_path(trial_dir)
    if not marker_path.exists():
        return None

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_verification_undrained_marker(
    trial_dir: Path,
    *,
    verification_kind: VerificationKind,
    expected_jobs: int,
    completed_results: int,
    missing_results: int,
    reason: str = "async_verification_drain_incomplete",
) -> Path:
    """Persist the shared drain marker atomically."""
    marker_path = verification_undrained_marker_path(trial_dir)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "verification_kind": verification_kind,
        "reason": reason,
        "expected_jobs": expected_jobs,
        "completed_results": completed_results,
        "missing_results": missing_results,
    }

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marker_path.parent,
            prefix=f"{marker_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp)
            temp_path = Path(tmp.name)
        temp_path.replace(marker_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise

    return marker_path
