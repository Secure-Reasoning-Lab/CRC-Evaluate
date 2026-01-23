"""Cleanup utilities for managing disk space during experiments.

This module handles artifact cleanup to manage disk space using a whitelist approach:
- Keep only essential files needed for reporting and analysis
- Delete all other files to free disk space
"""

import shutil
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Essential file/directory patterns to KEEP (whitelist)
# Everything else in the trial directory will be deleted
ESSENTIAL_PATTERNS: list[str] = [
    # Trial metadata and status
    "metadata.json",
    ".success",
    ".fail",
    "config.yaml",
    "execution.json",
    # Snapshots (contain all CRS outputs)
    "snapshot-*.tar.gz",
    "snapshot-*.complete",
    # Post-experiment data
    "final_coverage.json",
    # CRS outputs (redundant with snapshots but kept for convenience)
    "output/",
    # Input data for reproducibility
    "crs-input/",
    "hints/",
    "povs/",
]


def _resolve_symlinks_to_real(trial_dir: Path) -> None:
    """Resolve symlinks in whitelist to real files/directories.

    Some whitelisted items (e.g., output/) may be symlinks to locations
    in crs-build/. Before cleanup, we need to convert these to real
    files/directories by copying the content, otherwise deleting crs-build/
    would break the symlinks.

    Args:
        trial_dir: Path to trial output directory
    """
    for pattern in ESSENTIAL_PATTERNS:
        if pattern.endswith("/"):
            # Directory pattern
            name = pattern.rstrip("/")
            path = trial_dir / name
        elif "*" in pattern:
            # Glob pattern - check each match
            for path in trial_dir.glob(pattern):
                _resolve_single_symlink(path)
            continue
        else:
            # Exact file pattern
            path = trial_dir / pattern

        _resolve_single_symlink(path)


def _resolve_single_symlink(path: Path) -> None:
    """Resolve a single symlink to real file/directory.

    Args:
        path: Path that might be a symlink
    """
    # Check if it's a symlink (must check before exists() which follows symlinks)
    if not path.is_symlink():
        return

    target = path.resolve()
    if not target.exists():
        logger.warning(f"Symlink {path.name} points to non-existent target: {target}")
        path.unlink()
        return

    logger.info(f"Resolving symlink {path.name} -> {target}")
    # Remove the symlink first
    path.unlink()

    # Copy the actual content
    if target.is_dir():
        shutil.copytree(target, path)
    else:
        shutil.copy2(target, path)

    logger.debug(f"Converted symlink to real: {path.name}")


def cleanup_trial_directory(trial_dir: Path) -> None:
    """Clean up a trial directory, keeping only essential files.

    Uses whitelist approach: keeps only files matching ESSENTIAL_PATTERNS,
    deletes everything else.

    Args:
        trial_dir: Path to trial output directory
    """
    if not trial_dir.exists():
        logger.warning(f"Trial directory not found for cleanup: {trial_dir}")
        return

    logger.info(f"Cleaning up trial directory: {trial_dir}")

    # Resolve symlinks to real files/directories before cleanup
    # This prevents data loss when symlinks point to directories being deleted
    _resolve_symlinks_to_real(trial_dir)

    # Collect all paths to keep (resolve globs and directories)
    paths_to_keep: set[Path] = set()

    for pattern in ESSENTIAL_PATTERNS:
        if pattern.endswith("/"):
            # Directory pattern - keep entire directory tree
            dir_name = pattern.rstrip("/")
            dir_path = trial_dir / dir_name
            if dir_path.exists():
                paths_to_keep.add(dir_path)
                # Also add all contents recursively
                for child in dir_path.rglob("*"):
                    paths_to_keep.add(child)
        elif "*" in pattern:
            # Glob pattern
            for path in trial_dir.glob(pattern):
                paths_to_keep.add(path)
        else:
            # Exact file pattern
            path = trial_dir / pattern
            if path.exists():
                paths_to_keep.add(path)

    # Find all items in trial directory
    all_items = set(trial_dir.iterdir())

    # Determine items to delete (not in whitelist)
    items_to_delete = all_items - paths_to_keep

    # Also exclude items whose parents are in paths_to_keep (subdirectories)
    items_to_delete = {
        item
        for item in items_to_delete
        if not any(item.is_relative_to(kept) for kept in paths_to_keep if kept.is_dir())
    }

    deleted_count = 0
    bytes_freed = 0

    for item in items_to_delete:
        try:
            if item.is_dir():
                # Calculate directory size before deletion
                dir_size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                shutil.rmtree(item)
                bytes_freed += dir_size
                logger.debug(
                    f"Deleted directory: {item.name} ({dir_size / 1024 / 1024:.2f} MB)"
                )
            elif item.is_symlink():
                # Remove symlinks (don't count size)
                item.unlink()
                logger.debug(f"Deleted symlink: {item.name}")
            else:
                file_size = item.stat().st_size
                item.unlink()
                bytes_freed += file_size
                logger.debug(
                    f"Deleted file: {item.name} ({file_size / 1024 / 1024:.2f} MB)"
                )
            deleted_count += 1
        except Exception as e:
            logger.warning(f"Failed to delete {item}: {e}")

    if deleted_count > 0:
        logger.info(
            f"Cleaned {deleted_count} items, freed {bytes_freed / 1024 / 1024:.2f} MB in {trial_dir.name}"
        )
    else:
        logger.debug(f"No items to delete in {trial_dir.name}")


def copy_essential_files(trial_dir: Path, dest_dir: Path) -> None:
    """Copy essential files from trial directory to destination.

    Copies only files matching ESSENTIAL_PATTERNS, preserving directory structure.

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

    copied_count = 0
    bytes_copied = 0

    for pattern in ESSENTIAL_PATTERNS:
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
            # Glob pattern
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
            # Exact file pattern
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
