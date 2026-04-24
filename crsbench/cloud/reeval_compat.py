"""Shared compatibility helpers for cloud re-eval bundles and remote runners."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable


def discover_git_revision(cwd: Path | None = None) -> str | None:
    """Return the current git revision when available."""
    resolved_cwd = cwd or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=resolved_cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    revision = (result.stdout or "").strip()
    return revision or None


def build_compatibility_record(
    *,
    normalized_config: dict[str, Any],
    benchmark_names: Iterable[str],
    source_runtime_revision: str | None,
) -> dict[str, Any]:
    """Build the compatibility record persisted in a cloud re-eval manifest."""
    return {
        "benchmark_names": sorted(set(benchmark_names)),
        "source_mode": normalized_config.get("source_mode") or "pkgs",
        "per_pov_verify_timeout": normalized_config.get("per_pov_verify_timeout")
        or 180,
        "patch_verify_variants": bool(
            normalized_config.get("patch_verify_variants", False)
        ),
        "inc_image_policy": normalized_config.get("inc_image_policy") or "auto",
        "inc_image_registry": normalized_config.get("inc_image_registry"),
        "inc_image_max_pull_bytes": normalized_config.get("inc_image_max_pull_bytes"),
        "inc_image_pull_timeout_sec": normalized_config.get(
            "inc_image_pull_timeout_sec"
        ),
        "local_image_prefix": normalized_config.get("project_image_prefix"),
        "source_runtime_revision": source_runtime_revision,
    }
