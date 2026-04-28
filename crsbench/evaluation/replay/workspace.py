from __future__ import annotations

import importlib
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from crsbench.evaluation.replay.projects import sync_managed_projects_root

try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover
    fcntl = None


def _cached_oss_fuzz_path(cache_root: Path) -> Path:
    return cache_root.resolve() / "oss-fuzz-helper"


def _cached_projects_checkout_path(cache_root: Path) -> Path:
    return cache_root.resolve() / "oss-fuzz-projects"


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


@contextmanager
def _cache_lock(lock_path: Path):
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Replay cache bootstrap requires fcntl support")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_cached_oss_fuzz_workspace(
    cache_root: Path,
    *,
    seed_oss_fuzz_root: Path,
) -> Path:
    cache_root = Path(cache_root).resolve()
    seed_oss_fuzz_root = Path(seed_oss_fuzz_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    target = _cached_oss_fuzz_path(cache_root)
    helper_path = target / "infra" / "helper.py"
    if helper_path.is_file():
        return target

    with _cache_lock(cache_root / ".locks" / "oss-fuzz-helper.lock"):
        if helper_path.is_file():
            return target

        temp_target = Path(
            tempfile.mkdtemp(prefix=".oss-fuzz-helper.tmp-", dir=cache_root)
        )
        try:
            shutil.copytree(
                seed_oss_fuzz_root,
                temp_target,
                symlinks=True,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "build",
                    ".crsbench-locks",
                    "__pycache__",
                    "projects",
                ),
            )
            (temp_target / "projects").mkdir(parents=True, exist_ok=True)
            _remove_path(target)
            temp_target.replace(target)
        except Exception:
            _remove_path(temp_target)
            raise
        return target


def resolve_cache_projects_root(cache_root: Path, *, sync_projects: bool) -> Path:
    cache_root = Path(cache_root).resolve()
    checkout_path = _cached_projects_checkout_path(cache_root)
    projects_root = checkout_path / "projects"
    if projects_root.exists() and not sync_projects:
        return projects_root.resolve()

    with _cache_lock(cache_root / ".locks" / "oss-fuzz-projects.lock"):
        if projects_root.exists() and not sync_projects:
            return projects_root.resolve()
        return sync_managed_projects_root(checkout_path)
