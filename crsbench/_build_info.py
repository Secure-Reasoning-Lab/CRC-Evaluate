"""Build information and git commit tracking for CRSBench.

This module provides git commit information from three sources:
1. CRSBench (main repository) - from setuptools-scm generated _version.py
2. oss-crs (submodule) - build-time + runtime detection
3. oss-fuzz (managed third_party sparse checkout) - build-time + runtime detection
   with CLI override support

Uses setuptools-scm for main repo + build-time/runtime hybrid for submodules.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RepoCommit:
    """Git commit information for a single repository."""

    name: str
    commit: str
    dirty: bool = False

    def short_commit(self) -> str:
        """Return short commit hash (8 chars)."""
        if self.commit == "unknown":
            return "unknown"
        return self.commit[:8]


@dataclass
class BuildInfo:
    """Complete build information for CRSBench."""

    crsbench: RepoCommit
    oss_crs: RepoCommit
    oss_fuzz: RepoCommit
    build_timestamp: str
    version: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "crsbench": {
                "commit": self.crsbench.commit,
                "short_commit": self.crsbench.short_commit(),
                "dirty": self.crsbench.dirty,
            },
            "oss_crs": {
                "commit": self.oss_crs.commit,
                "short_commit": self.oss_crs.short_commit(),
                "dirty": self.oss_crs.dirty,
            },
            "oss_fuzz": {
                "commit": self.oss_fuzz.commit,
                "short_commit": self.oss_fuzz.short_commit(),
                "dirty": self.oss_fuzz.dirty,
            },
            "build_timestamp": self.build_timestamp,
        }


def _get_git_commit(repo_path: Path) -> tuple[str, bool]:
    """Get git commit and dirty status for a repository.

    Args:
        repo_path: Path to git repository

    Returns:
        Tuple of (commit_hash, is_dirty)
    """
    try:
        if not repo_path.exists():
            return ("unknown", False)

        # Get commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ("unknown", False)

        commit = result.stdout.strip()

        # Check if dirty
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = bool(result.stdout.strip())

        return (commit, dirty)
    except Exception:
        return ("unknown", False)


def get_build_info(oss_fuzz_path: Optional[Path] = None) -> BuildInfo:
    """Get build information from setuptools-scm + submodule detection.

    Args:
        oss_fuzz_path: Optional custom path to oss-fuzz (for CLI override)

    Returns:
        BuildInfo with commit information from all three repositories
    """
    from datetime import datetime

    # Get version and commit info from setuptools-scm
    try:
        from crsbench._version import __version__, version  # type: ignore

        # setuptools-scm version might have commit info like "0.1.0.dev5+gabc1234"
        scm_version = version
        scm_commit = "unknown"

        # Extract commit hash if present (format: 0.1.0.dev5+gabc1234)
        if "+" in scm_version and scm_version.split("+")[1].startswith("g"):
            scm_commit = scm_version.split("+")[1][1:]  # Remove 'g' prefix

        # Fall back to runtime detection if scm doesn't have commit
        if scm_commit == "unknown":
            crsbench_root = Path(__file__).parent.parent.resolve()
            scm_commit, crsbench_dirty = _get_git_commit(crsbench_root)
        else:
            # Check dirty status
            crsbench_root = Path(__file__).parent.parent.resolve()
            _, crsbench_dirty = _get_git_commit(crsbench_root)

    except ImportError:
        # Fallback if _version.py doesn't exist (development mode without install)
        __version__ = "0.1.0.dev0+unknown"
        crsbench_root = Path(__file__).parent.parent.resolve()
        scm_commit, crsbench_dirty = _get_git_commit(crsbench_root)

    # Get submodule commits: try build-time first, then runtime
    try:
        from crsbench._submodule_commits import (  # type: ignore
            OSS_CRS_COMMIT,
            OSS_FUZZ_COMMIT,
        )

        oss_crs_build_commit = OSS_CRS_COMMIT
        oss_fuzz_build_commit = OSS_FUZZ_COMMIT
    except ImportError:
        # No build-time commits available (development mode)
        oss_crs_build_commit = "unknown"
        oss_fuzz_build_commit = "unknown"

    # Runtime detection for submodules
    crsbench_root = Path(__file__).parent.parent.resolve()

    # oss-crs: try runtime, fall back to build-time
    oss_crs_path = crsbench_root / "oss-crs"
    oss_crs_commit, oss_crs_dirty = _get_git_commit(oss_crs_path)
    if oss_crs_commit == "unknown":
        oss_crs_commit = oss_crs_build_commit
        oss_crs_dirty = False

    # oss-fuzz: use provided path or default managed checkout, with build-time fallback
    if oss_fuzz_path is None:
        oss_fuzz_path = crsbench_root / "third_party" / "oss-fuzz"

    oss_fuzz_commit, oss_fuzz_dirty = _get_git_commit(oss_fuzz_path)
    if oss_fuzz_commit == "unknown":
        oss_fuzz_commit = oss_fuzz_build_commit
        oss_fuzz_dirty = False

    return BuildInfo(
        crsbench=RepoCommit("crsbench", scm_commit, crsbench_dirty),
        oss_crs=RepoCommit("oss-crs", oss_crs_commit, oss_crs_dirty),
        oss_fuzz=RepoCommit("oss-fuzz", oss_fuzz_commit, oss_fuzz_dirty),
        build_timestamp=datetime.now().isoformat(),
        version=__version__,
    )


# Cached instance
_build_info: Optional[BuildInfo] = None


def get_cached_build_info(oss_fuzz_path: Optional[Path] = None) -> BuildInfo:
    """Get cached build info (computed once per process).

    Args:
        oss_fuzz_path: Optional custom path to oss-fuzz (invalidates cache)

    Returns:
        BuildInfo with commit information
    """
    global _build_info

    # Invalidate cache if custom oss-fuzz path provided
    if oss_fuzz_path is not None:
        return get_build_info(oss_fuzz_path)

    # Use cached value if available
    if _build_info is None:
        _build_info = get_build_info()
    return _build_info
