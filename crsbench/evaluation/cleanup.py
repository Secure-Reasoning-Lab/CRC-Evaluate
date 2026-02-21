"""Cleanup utilities for managing disk space during experiments.

This module handles artifact cleanup to manage disk space using a whitelist approach:
- Keep only essential files needed for reporting and analysis
- Delete all other files to free disk space
"""

import shutil
import subprocess
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _rmtree_with_docker(path: Path) -> bool:
    """Remove directory using Docker for root-owned files.

    Docker containers often create files owned by root that cannot be deleted
    by regular users. This function uses a Docker container to delete such files.

    Args:
        path: Directory to remove

    Returns:
        True if deletion succeeded, False otherwise
    """
    try:
        # Use alpine image to delete the directory
        # Mount parent directory and delete the target
        parent = path.parent.resolve()
        name = path.name
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{parent}:/mnt",
                "alpine",
                "rm",
                "-rf",
                f"/mnt/{name}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            logger.debug(f"Deleted with Docker: {path}")
            return True
        logger.warning(f"Docker rm failed: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"Docker rm timed out for {path}")
        return False
    except FileNotFoundError:
        logger.warning("Docker not available for cleanup")
        return False
    except Exception as e:
        logger.warning(f"Docker rm failed for {path}: {e}")
        return False


# Directories to EXCLUDE from copying (blacklist)
# These contain large build artifacts that aren't needed for results
EXCLUDE_PATTERNS: list[str] = [
    "oss-crs-workdir",
    "staged",
]


def _resolve_symlinks_pointing_to_excluded(trial_dir: Path) -> None:
    """Resolve symlinks that point to excluded directories.

    Some items (e.g., output/) may be symlinks to locations inside
    excluded dirs (oss-crs-workdir/, staged/).  Before cleanup,
    we convert these to real files/directories so deleting the
    excluded dirs does not break the symlinks.

    Args:
        trial_dir: Path to trial output directory
    """
    for item in trial_dir.iterdir():
        if not item.is_symlink():
            continue

        # Check if symlink points to an excluded directory
        try:
            target = item.resolve()
            target_rel = target.relative_to(trial_dir)
            # Check if target is inside an excluded directory
            if any(part in EXCLUDE_PATTERNS for part in target_rel.parts):
                logger.info(f"Resolving symlink {item.name} -> {target}")
                item.unlink()
                if target.is_dir():
                    shutil.copytree(target, item)
                elif target.exists():
                    shutil.copy2(target, item)
                logger.debug(f"Converted symlink to real: {item.name}")
        except (ValueError, OSError):
            # Target not relative to trial_dir or other error
            pass


def cleanup_trial_directory(trial_dir: Path) -> None:
    """Clean up a trial directory by removing blacklisted items.

    Uses blacklist approach: deletes only items matching EXCLUDE_PATTERNS.

    Args:
        trial_dir: Path to trial output directory
    """
    if not trial_dir.exists():
        logger.warning(f"Trial directory not found for cleanup: {trial_dir}")
        return

    logger.info(f"Cleaning up trial directory: {trial_dir}")

    # Resolve symlinks pointing to excluded directories before cleanup
    _resolve_symlinks_pointing_to_excluded(trial_dir)

    deleted_count = 0

    for pattern in EXCLUDE_PATTERNS:
        item = trial_dir / pattern
        if not item.exists():
            continue

        try:
            if item.is_dir():
                # Try normal deletion first
                try:
                    shutil.rmtree(item)
                    logger.debug(f"Deleted directory: {item.name}")
                except PermissionError:
                    # Try Docker for root-owned files (e.g., from Docker containers)
                    if _rmtree_with_docker(item):
                        logger.debug(f"Deleted directory with Docker: {item.name}")
                    else:
                        logger.warning(
                            f"Failed to delete {item.name} (permission denied, Docker fallback failed)"
                        )
                        continue
            else:
                item.unlink()
                logger.debug(f"Deleted file: {item.name}")
            deleted_count += 1
        except Exception as e:
            logger.warning(f"Failed to delete {item}: {e}")

    if deleted_count > 0:
        logger.info(f"Cleaned {deleted_count} items in {trial_dir.name}")
    else:
        logger.debug(f"No items to delete in {trial_dir.name}")


def copy_trial_results(trial_dir: Path, dest_dir: Path) -> None:
    """Copy trial directory to destination, excluding blacklisted patterns.

    Uses blacklist approach: copies everything except EXCLUDE_PATTERNS.

    Args:
        trial_dir: Source trial directory
        dest_dir: Destination directory
    """
    if not trial_dir.exists():
        logger.warning(f"Trial directory not found for copying: {trial_dir}")
        return

    logger.info(f"Copying trial results from {trial_dir} to {dest_dir}")

    # Resolve symlinks pointing to excluded directories (e.g., output/ -> crs-build/...)
    # so their content is copied as real files instead of broken symlinks.
    _resolve_symlinks_pointing_to_excluded(trial_dir)

    def ignore_patterns(_directory: str, files: list[str]) -> set[str]:
        """Return set of files/directories to ignore."""
        return {f for f in files if f in EXCLUDE_PATTERNS}

    try:
        shutil.copytree(
            trial_dir,
            dest_dir,
            ignore=ignore_patterns,
            dirs_exist_ok=True,
        )
        logger.info(f"Copied trial results to {dest_dir}")
    except Exception as e:
        logger.warning(f"Failed to copy trial results: {e}")
