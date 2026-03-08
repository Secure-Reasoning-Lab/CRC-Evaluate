"""Helpers for deterministic, collision-resistant trial identifiers."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pathlib import Path


def _sanitize_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return token or "unknown"


def build_trial_uid(
    *,
    benchmark: str,
    harness: str,
    trial_dir: Path,
    experiment: Optional[str] = None,
) -> str:
    """Build a stable UID for queue routing and worker-local isolation.

    Includes readable benchmark/harness components and a hash of the absolute
    trial path to prevent collisions between similarly named trials.
    """
    experiment_token = _sanitize_token(experiment or "exp")
    benchmark_token = _sanitize_token(benchmark)
    harness_token = _sanitize_token(harness)
    trial_name = _sanitize_token(trial_dir.name)
    path_hash = hashlib.sha1(str(trial_dir.resolve()).encode()).hexdigest()[:12]
    return (
        f"{experiment_token}__{benchmark_token}__{harness_token}"
        f"__{trial_name}__{path_hash}"
    )
