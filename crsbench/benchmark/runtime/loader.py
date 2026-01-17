"""Benchmark source loading for CRS evaluation.

This module provides a unified interface for loading benchmark source code,
handling both:
- Bundled sources (pkgs/ tarballs in Docker image)
- Git-cloned sources (from main_repo)

The loader abstracts the source resolution logic so executors don't need
to duplicate pkgs/ detection code.

For verification (verify/patch-verify), use prepare_source_from_bundle() to
extract and prepare source from pkgs/ with optional ref.diff application.
"""

import os
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

from crsbench.benchmark.runtime.models import BenchmarkSource
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def load_benchmark_source(
    benchmark_path: Path,
    dest_dir: Optional[Path] = None,
    *,
    mode: Optional[str] = None,
    verbose: bool = False,
) -> BenchmarkSource:
    """Load benchmark source - from pkgs/ or by cloning.

    This is the single source of truth for determining how to provide
    source code to a CRS. It checks for bundled source first, then
    falls back to git cloning.

    Args:
        benchmark_path: Path to benchmark directory
        dest_dir: Destination directory for cloned source (required if not bundled)
        mode: Benchmark mode ("delta" or "full") for commit selection
        verbose: Enable verbose logging

    Returns:
        BenchmarkSource with path and is_bundled status

    Raises:
        RuntimeError: If source cannot be obtained (no pkgs/ and clone fails)

    Example:
        source = load_benchmark_source(benchmark_path, trial_dir / "src")
        if source.requires_source_path:
            cmd.extend(["--source-path", str(source.path)])
    """
    benchmark_path = Path(benchmark_path)

    # Check for bundled source in pkgs/
    if has_bundled_source(benchmark_path):
        logger.info("Using bundled source from pkgs/ (no source-path needed)")
        return BenchmarkSource(path=None, is_bundled=True)

    # Clone source (requires dest_dir)
    if dest_dir is None:
        raise RuntimeError(
            "No bundled source (pkgs/) and no dest_dir provided for cloning"
        )

    from crsbench.utils.repo_manager import ensure_project_repository

    logger.info(f"Cloning source to: {dest_dir}")
    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_path),
        project_dir=str(dest_dir),
        mode=mode,
        verbose=verbose,
    )

    if not source_path:
        raise RuntimeError(
            f"Failed to obtain source code for {benchmark_path.name}. "
            "Check that project.yaml has valid main_repo or provide source manually."
        )

    logger.info(f"Using cloned source from: {source_path}")
    return BenchmarkSource(path=Path(source_path), is_bundled=False)


def has_bundled_source(benchmark_path: Path) -> bool:
    """Check if benchmark has bundled source in pkgs/.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        True if pkgs/ exists with at least one tarball
    """
    pkgs_dir = benchmark_path / "pkgs"
    return pkgs_dir.exists() and any(pkgs_dir.glob("*.tar.gz"))


def prepare_source_from_bundle(
    benchmark_path: Path,
    dest_dir: Path,
    source_name: str,
    *,
    apply_ref_diff: bool = False,
) -> Optional[Path]:
    """Prepare source by extracting tarball from pkgs/.

    This is used by verification (verify/patch-verify) to prepare source
    from bundled tarballs instead of cloning from git.

    Args:
        benchmark_path: Path to benchmark directory
        dest_dir: Destination directory to extract source into
        source_name: Name of source (e.g., "curl") - matches tarball name
        apply_ref_diff: If True, apply .aixcc/ref.diff after extraction
            (for deltaref, allpatched, cpvN variants)

    Returns:
        Path to extracted source directory, or None if failed

    Example:
        # For deltabase variant (no ref.diff needed)
        src = prepare_source_from_bundle(bench_path, tmp, "curl")

        # For deltaref variant (apply ref.diff)
        src = prepare_source_from_bundle(bench_path, tmp, "curl", apply_ref_diff=True)
    """
    benchmark_path = Path(benchmark_path)
    dest_dir = Path(dest_dir)

    # Find tarball
    tarball_path = benchmark_path / "pkgs" / f"{source_name}.tar.gz"
    if not tarball_path.exists():
        logger.error(f"Tarball not found: {tarball_path}")
        return None

    # Create destination directory
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Extract tarball
    logger.info(f"Extracting {tarball_path.name} to {dest_dir}")
    try:
        with tarfile.open(tarball_path, "r:gz") as tar:
            tar.extractall(dest_dir)
    except Exception as e:
        logger.error(f"Failed to extract tarball: {e}")
        return None

    # The extracted source is in dest_dir/source_name
    source_path = dest_dir / source_name
    if not source_path.exists():
        logger.error(f"Expected source directory not found: {source_path}")
        return None

    # Ensure git is initialized (some tarballs may not have .git)
    if not (source_path / ".git").exists():
        logger.info("Initializing git repository (tarball has no .git)")
        if not _init_git_repo(source_path):
            logger.error("Failed to initialize git repository")
            return None

    # Apply ref.diff if requested (for deltaref-like variants)
    if apply_ref_diff:
        ref_diff_path = benchmark_path / ".aixcc" / "ref.diff"
        if ref_diff_path.exists():
            logger.info("Applying ref.diff to get ref commit state")
            if not _apply_diff(source_path, ref_diff_path):
                logger.error("Failed to apply ref.diff")
                return None
            # Commit ref.diff changes so subsequent patches can be applied
            if not _commit_changes(source_path, "Apply ref.diff"):
                logger.error("Failed to commit ref.diff changes")
                return None
        else:
            logger.warning(f"ref.diff not found at {ref_diff_path}, skipping")

    logger.info(f"Source prepared at: {source_path}")
    return source_path


def get_ref_diff_path(benchmark_path: Path) -> Optional[Path]:
    """Get path to ref.diff if it exists.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Path to ref.diff, or None if not found
    """
    ref_diff = Path(benchmark_path) / ".aixcc" / "ref.diff"
    return ref_diff if ref_diff.exists() else None


def _init_git_repo(repo_path: Path) -> bool:
    """Initialize a git repository with initial commit.

    Args:
        repo_path: Path to directory to initialize

    Returns:
        True if successful
    """
    try:
        # Git init
        result = subprocess.run(
            ["git", "init"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"git init failed: {result.stderr}")
            return False

        # Add all files
        result = subprocess.run(
            ["git", "add", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"git add failed: {result.stderr}")
            return False

        # Create initial commit (disable GPG signing to avoid timeouts)
        result = subprocess.run(
            ["git", "commit", "--no-gpg-sign", "-m", "Initial commit from tarball"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "CRSBench",
                "GIT_AUTHOR_EMAIL": "crsbench@example.com",
                "GIT_COMMITTER_NAME": "CRSBench",
                "GIT_COMMITTER_EMAIL": "crsbench@example.com",
            },
        )
        if result.returncode != 0:
            logger.error(f"git commit failed: {result.stderr}")
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to init git repo: {e}")
        return False


def _commit_changes(repo_path: Path, message: str) -> bool:
    """Commit all changes in the repository.

    Args:
        repo_path: Path to git repository
        message: Commit message

    Returns:
        True if successful
    """
    try:
        # Add all changes
        result = subprocess.run(
            ["git", "add", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"git add failed: {result.stderr}")
            return False

        # Commit
        result = subprocess.run(
            ["git", "commit", "--no-gpg-sign", "-m", message],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "CRSBench",
                "GIT_AUTHOR_EMAIL": "crsbench@example.com",
                "GIT_COMMITTER_NAME": "CRSBench",
                "GIT_COMMITTER_EMAIL": "crsbench@example.com",
            },
        )
        if result.returncode != 0:
            logger.error(f"git commit failed: {result.stderr}")
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to commit changes: {e}")
        return False


def _apply_diff(repo_path: Path, diff_path: Path) -> bool:
    """Apply a diff file to a repository.

    Args:
        repo_path: Path to git repository
        diff_path: Path to diff file

    Returns:
        True if successful
    """
    try:
        # Use absolute path since git runs from repo_path
        abs_diff_path = Path(diff_path).resolve()
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(abs_diff_path)],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"git apply failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to apply diff: {e}")
        return False
