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
    dest_dir: Path,
    *,
    source_mode: str = "main_repo",
    mode: Optional[str] = None,
    verbose: bool = False,
) -> BenchmarkSource:
    """Load benchmark source based on source_mode.

    Args:
        benchmark_path: Path to benchmark directory
        dest_dir: Destination directory for extracted/cloned source
        source_mode: "main_repo" (clone from git) or "pkgs" (use bundled tarball)
        mode: Benchmark mode ("delta" or "full") for commit selection
        verbose: Enable verbose logging

    Returns:
        BenchmarkSource with path and is_bundled status

    Raises:
        RuntimeError: If source cannot be obtained
        ValueError: If source_mode is invalid

    Example:
        # Clone from main_repo (default)
        source = load_benchmark_source(benchmark_path, trial_dir / "src")

        # Use bundled pkgs/
        source = load_benchmark_source(benchmark_path, trial_dir / "src", source_mode="pkgs")
    """
    benchmark_path = Path(benchmark_path)

    if source_mode not in ("main_repo", "pkgs"):
        raise ValueError(
            f"Invalid source_mode: {source_mode}. Use 'main_repo' or 'pkgs'"
        )

    if source_mode == "pkgs":
        return _load_from_pkgs(benchmark_path, dest_dir, mode=mode)

    return _load_from_main_repo(benchmark_path, dest_dir, mode=mode, verbose=verbose)


def _load_from_pkgs(
    benchmark_path: Path,
    dest_dir: Path,
    *,
    mode: Optional[str] = None,
) -> BenchmarkSource:
    """Load source from bundled pkgs/ tarball.

    The tarball already has the correct commit structure (done at packaging time):
    - Both modes: 1 squashed commit at vulnerable state
    - Delta mode provides ref.diff as hint (not via git history)

    Args:
        benchmark_path: Path to benchmark directory
        dest_dir: Destination directory to extract source into
        mode: Benchmark mode (unused - kept for API compatibility)
    """
    _ = mode  # Unused - commit structure is determined at packaging time

    if not has_bundled_source(benchmark_path):
        raise RuntimeError(
            f"No bundled source in {benchmark_path}/pkgs/. "
            "Use --source main_repo or run 'crsbench bundle' first."
        )

    # Determine source name from tarball
    pkgs_dir = benchmark_path / "pkgs"
    tarballs = list(pkgs_dir.glob("*.tar.gz"))
    if not tarballs:
        raise RuntimeError(f"No tarballs found in {pkgs_dir}")

    # Use first tarball (assume single main source)
    tarball = tarballs[0]
    source_name = tarball.stem
    if source_name.endswith(".tar"):
        source_name = source_name[:-4]

    logger.info(f"Extracting bundled source from {tarball.name}")
    source_path = prepare_source_from_bundle(
        benchmark_path,
        dest_dir,
        source_name,
    )

    if not source_path:
        raise RuntimeError(f"Failed to extract source from {tarball}")

    logger.info(f"Using bundled source from: {source_path}")
    return BenchmarkSource(path=source_path, is_bundled=True)


def _load_from_main_repo(
    benchmark_path: Path,
    dest_dir: Path,
    *,
    mode: Optional[str] = None,
    verbose: bool = False,
) -> BenchmarkSource:
    """Load source by cloning from main_repo."""
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
) -> Optional[Path]:
    """Prepare source by extracting tarball from pkgs/.

    The bundled tarball already has the correct commit structure:
    - Both modes: 1 squashed commit at vulnerable state
    - Delta mode provides ref.diff as hint (not via git history)

    No post-processing needed - just extract and use.

    Args:
        benchmark_path: Path to benchmark directory
        dest_dir: Destination directory to extract source into
        source_name: Name of source (e.g., "curl") - matches tarball name

    Returns:
        Path to extracted source directory, or None if failed
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
        # Common env to prevent any interactive prompts
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "CRSBench",
            "GIT_AUTHOR_EMAIL": "crsbench@example.com",
            "GIT_COMMITTER_NAME": "CRSBench",
            "GIT_COMMITTER_EMAIL": "crsbench@example.com",
            "GIT_TERMINAL_PROMPT": "0",  # Prevent credential prompts
        }

        # Git init
        result = subprocess.run(
            ["git", "init"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,  # Prevent interactive prompts
            env=git_env,
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
            stdin=subprocess.DEVNULL,
            env=git_env,
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
            stdin=subprocess.DEVNULL,
            env=git_env,
        )
        if result.returncode != 0:
            logger.error(f"git commit failed: {result.stderr}")
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to init git repo: {e}")
        return False
