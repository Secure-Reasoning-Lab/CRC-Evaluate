"""Warnings for persisted storage roots placed under /tmp."""

import os
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

_TMP_ROOT = Path("/tmp")
_warned_paths: set[str] = set()


def _normalize_storage_path(path: Path | str) -> Path:
    """Return a lexical normalization that avoids symlink-dependent behavior."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return Path(os.path.normpath(os.fspath(candidate)))

    try:
        cwd = Path.cwd()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.normpath(os.fspath(candidate)))

    return Path(os.path.normpath(os.fspath(cwd / candidate)))


def _is_under_tmp(path: Path) -> bool:
    """Return True for /tmp and descendants, but not similarly named paths."""
    return path == _TMP_ROOT or _TMP_ROOT in path.parents


def warn_if_persisted_storage_path(path: Path | str) -> bool:
    """Warn once when a persisted storage root is placed under /tmp."""
    normalized = _normalize_storage_path(path)
    if not _is_under_tmp(normalized):
        return False

    key = str(normalized)
    if key in _warned_paths:
        return False

    _warned_paths.add(key)
    logger.warning(
        "Persisted storage path '{}' is under /tmp. /tmp is often tmpfs-backed, "
        "so it can consume RAM; for large-scale experiments, use another location.",
        normalized,
    )
    return True


def warn_for_persisted_storage_roots(
    *,
    experiment_filestore: Path | str | None,
    report_filestore: Path | str | None,
    copy_results_after_trial: bool = False,
    results_filestore: Path | str | None = None,
    remote_experiment_root: Path | str | None = None,
) -> None:
    """Warn for the effective persisted storage roots used by a run."""
    if experiment_filestore is not None:
        warn_if_persisted_storage_path(experiment_filestore)

    if report_filestore is not None:
        warn_if_persisted_storage_path(report_filestore)

    if copy_results_after_trial and results_filestore is not None:
        warn_if_persisted_storage_path(results_filestore)

    if remote_experiment_root is not None:
        warn_if_persisted_storage_path(remote_experiment_root)
