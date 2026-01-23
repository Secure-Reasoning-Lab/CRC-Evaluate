"""Cleanup utilities for managing disk space during experiments.

This module handles artifact cleanup to manage disk space:
- Per-trial cleanup: Remove bulky intermediate files after each trial
- Post-experiment cleanup: Keep only essential results after all trials complete
- Optional results copying: Copy essential files to separate location
"""

import shutil
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_essential_files() -> list[str]:
    """Return list of essential file/directory patterns to keep.

    Essential files are those needed for reporting and analysis:
    - Trial metadata and status markers
    - Snapshots containing all CRS outputs
    - Final coverage data
    - Experiment configuration
    - CRS outputs (redundant with snapshots but kept for convenience)
    - Input data (POVs, hints) for reproducibility

    Returns:
        List of file/directory patterns to preserve
    """
    return [
        "metadata.json",  # Trial identification
        "snapshot-*.tar.gz",  # Compressed snapshots with all data
        "snapshot-*.complete",  # Snapshot completion markers
        "snapshot-latest.tar.gz",  # Symlink to latest snapshot
        "snapshot-final.tar.gz",  # Symlink to final snapshot
        ".success",  # Trial success marker
        ".fail",  # Trial failure marker
        "config.yaml",  # Experiment config (small, useful for reference)
        "execution.json",  # Execution metadata
        "output/",  # CRS outputs (redundant with snapshots but kept for convenience)
        "final_coverage.json",  # Post-experiment coverage data
        "crs-input/",  # Input POVs for patch generation
        "hints/",  # Input hints
        "povs/",  # Input POVs
    ]


def cleanup_trial_artifacts(trial_dir: Path) -> None:
    """Delete non-essential artifacts from a trial directory.

    Removes bulky intermediate files while preserving essential data
    needed for reporting and analysis.

    Args:
        trial_dir: Path to trial output directory

    Files deleted:
        - crs-output.log: Already captured in snapshots
        - llm-usage.json, llm-logs.json: Already captured in snapshots
        - patch-verify/: Intermediate patch verification working directory
        - coverage/: Intermediate coverage data
    """
    if not trial_dir.exists():
        logger.warning(f"Trial directory not found for cleanup: {trial_dir}")
        return

    logger.info(f"Cleaning up trial artifacts in {trial_dir}")

    # Files/directories to delete (non-essential)
    deletable_patterns = [
        "crs-output.log",  # Already in snapshots
        "llm-usage.json",  # Already in snapshots
        "llm-logs.json",  # Already in snapshots
        "patch-verify/",  # Intermediate working dir
        "coverage/",  # Intermediate coverage data
    ]

    deleted_count = 0
    bytes_freed = 0

    for pattern in deletable_patterns:
        path = trial_dir / pattern
        if path.exists():
            try:
                if path.is_dir():
                    # Calculate directory size before deletion
                    dir_size = sum(
                        f.stat().st_size for f in path.rglob("*") if f.is_file()
                    )
                    shutil.rmtree(path)
                    bytes_freed += dir_size
                    logger.debug(
                        f"Deleted directory: {path} ({dir_size / 1024 / 1024:.2f} MB)"
                    )
                else:
                    file_size = path.stat().st_size
                    path.unlink()
                    bytes_freed += file_size
                    logger.debug(
                        f"Deleted file: {path} ({file_size / 1024 / 1024:.2f} MB)"
                    )
                deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")

    if deleted_count > 0:
        logger.info(
            f"Cleaned {deleted_count} items, freed {bytes_freed / 1024 / 1024:.2f} MB in {trial_dir.name}"
        )
    else:
        logger.debug(f"No deletable artifacts found in {trial_dir.name}")


def cleanup_build_variants(oss_fuzz_path: Path, benchmark_name: str, mode: str) -> None:
    """Delete build variant directories for a benchmark.

    Removes build outputs to free disk space. These are large directories
    containing compiled binaries and intermediate build files.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
        benchmark_name: Benchmark name (e.g., "sanity-mock-c-delta-01")
        mode: Evaluation mode ("delta" or "full")

    Directories deleted:
        - build/out/{benchmark}-{mode}-*/: Build output directories
        - build/work/{benchmark}-{mode}-*/: Build working directories
        - build/src/{benchmark}-{mode}-*/: Build source directories
    """
    if not oss_fuzz_path.exists():
        logger.warning(f"oss-fuzz path not found for cleanup: {oss_fuzz_path}")
        return

    logger.info(f"Cleaning up build variants for {benchmark_name} ({mode} mode)")

    # Build directories to clean
    build_dirs = [
        oss_fuzz_path / "build" / "out",
        oss_fuzz_path / "build" / "work",
        oss_fuzz_path / "build" / "src",
    ]

    # Pattern to match: {benchmark}-{mode}-{variant}
    # Examples: sanity-mock-c-delta-01-delta-ASAN, sanity-mock-c-delta-01-delta-UBSAN
    variant_pattern = f"{benchmark_name}-{mode}-*"

    deleted_count = 0
    bytes_freed = 0

    for build_dir in build_dirs:
        if not build_dir.exists():
            continue

        # Find matching variant directories
        for variant_dir in build_dir.glob(variant_pattern):
            if variant_dir.is_dir():
                try:
                    # Calculate directory size before deletion
                    dir_size = sum(
                        f.stat().st_size for f in variant_dir.rglob("*") if f.is_file()
                    )
                    shutil.rmtree(variant_dir)
                    bytes_freed += dir_size
                    logger.debug(
                        f"Deleted build variant: {variant_dir} ({dir_size / 1024 / 1024:.2f} MB)"
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete {variant_dir}: {e}")

    if deleted_count > 0:
        logger.info(
            f"Cleaned {deleted_count} build variants, freed {bytes_freed / 1024 / 1024 / 1024:.2f} GB for {benchmark_name}"
        )
    else:
        logger.debug(f"No build variants found for {benchmark_name} ({mode} mode)")


def copy_essential_files(trial_dir: Path, dest_dir: Path) -> None:
    """Copy essential files from trial directory to results_filestore.

    Preserves the same directory structure in the destination.

    Args:
        trial_dir: Source trial directory
        dest_dir: Destination directory (mirrors trial structure)
    """
    if not trial_dir.exists():
        logger.warning(f"Trial directory not found for copying: {trial_dir}")
        return

    logger.info(f"Copying essential files from {trial_dir} to {dest_dir}")

    # Ensure destination directory exists
    dest_dir.mkdir(parents=True, exist_ok=True)

    essential_patterns = get_essential_files()
    copied_count = 0
    bytes_copied = 0

    for pattern in essential_patterns:
        # Handle both files and directories
        if pattern.endswith("/"):
            # Directory pattern
            dir_name = pattern.rstrip("/")
            src_path = trial_dir / dir_name
            if src_path.exists() and src_path.is_dir():
                dest_path = dest_dir / dir_name
                try:
                    shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
                    dir_size = sum(
                        f.stat().st_size for f in dest_path.rglob("*") if f.is_file()
                    )
                    bytes_copied += dir_size
                    copied_count += 1
                    logger.debug(
                        f"Copied directory: {dir_name} ({dir_size / 1024 / 1024:.2f} MB)"
                    )
                except Exception as e:
                    logger.warning(f"Failed to copy directory {src_path}: {e}")
        elif "*" in pattern:
            # Glob pattern (e.g., snapshot-*.tar.gz)
            for src_path in trial_dir.glob(pattern):
                dest_path = dest_dir / src_path.name
                try:
                    if src_path.is_symlink():
                        # Preserve symlinks
                        link_target = src_path.readlink()
                        if dest_path.exists():
                            dest_path.unlink()
                        dest_path.symlink_to(link_target)
                        logger.debug(f"Copied symlink: {src_path.name}")
                    else:
                        shutil.copy2(src_path, dest_path)
                        file_size = dest_path.stat().st_size
                        bytes_copied += file_size
                        logger.debug(
                            f"Copied file: {src_path.name} ({file_size / 1024 / 1024:.2f} MB)"
                        )
                    copied_count += 1
                except Exception as e:
                    logger.warning(f"Failed to copy {src_path}: {e}")
        else:
            # Regular file pattern
            src_path = trial_dir / pattern
            if src_path.exists() and src_path.is_file():
                dest_path = dest_dir / pattern
                try:
                    shutil.copy2(src_path, dest_path)
                    file_size = dest_path.stat().st_size
                    bytes_copied += file_size
                    copied_count += 1
                    logger.debug(
                        f"Copied file: {pattern} ({file_size / 1024 / 1024:.2f} MB)"
                    )
                except Exception as e:
                    logger.warning(f"Failed to copy {src_path}: {e}")

    if copied_count > 0:
        logger.info(
            f"Copied {copied_count} items ({bytes_copied / 1024 / 1024:.2f} MB) to {dest_dir}"
        )
    else:
        logger.warning(f"No essential files found to copy from {trial_dir}")
