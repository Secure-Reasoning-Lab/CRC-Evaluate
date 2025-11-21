"""
File migration utilities for converting benchmark file structure.

Handles copying and organizing files from Team-Atlanta format to RFC format.
"""

from crsbench.utils.logger import get_logger
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crsbench.migration.atlanta_to_rfc import MigrationContext


class FileMigrator:
    """Handles file copying and directory structure creation."""

    def __init__(self, dry_run: bool = False):
        """
        Initialize the file migrator.

        Args:
            dry_run: If True, log operations without actually copying files
        """
        self.dry_run = dry_run
        self.logger = get_logger(__name__)

    def copy_file(self, source: Path, target: Path, ctx: 'MigrationContext') -> bool:
        """
        Copy a file from source to target location.

        Args:
            source: Source file path
            target: Target file path
            ctx: Migration context for logging

        Returns:
            True if successful, False otherwise
        """
        if not source.exists():
            ctx.warnings.append(f"Source file not found: {source}")
            return False

        if not source.is_file():
            ctx.warnings.append(f"Source is not a file: {source}")
            return False

        try:
            if self.dry_run:
                self.logger.info(f"      [DRY RUN] Would copy: {source.name} -> {target}")
                return True

            # Create parent directory
            target.parent.mkdir(parents=True, exist_ok=True)

            # Copy file
            shutil.copy2(source, target)
            self.logger.debug(f"      Copied: {source.name} -> {target}")

            return True

        except Exception as e:
            error_msg = f"Failed to copy {source} to {target}: {str(e)}"
            ctx.errors.append(error_msg)
            self.logger.error(error_msg)
            return False

    def copy_directory(self, source: Path, target: Path, ctx: 'MigrationContext') -> bool:
        """
        Recursively copy a directory from source to target location.

        Args:
            source: Source directory path
            target: Target directory path
            ctx: Migration context for logging

        Returns:
            True if successful, False otherwise
        """
        if not source.exists():
            ctx.warnings.append(f"Source directory not found: {source}")
            return False

        if not source.is_dir():
            ctx.warnings.append(f"Source is not a directory: {source}")
            return False

        try:
            if self.dry_run:
                self.logger.info(f"      [DRY RUN] Would copy directory: {source.name} -> {target}")
                return True

            # Copy entire directory tree
            shutil.copytree(source, target, dirs_exist_ok=True)
            self.logger.debug(f"      Copied directory: {source.name} -> {target}")

            return True

        except Exception as e:
            error_msg = f"Failed to copy directory {source} to {target}: {str(e)}"
            ctx.errors.append(error_msg)
            self.logger.error(error_msg)
            return False

    def ensure_directory(self, path: Path, ctx: 'MigrationContext') -> bool:
        """
        Ensure a directory exists, creating it if necessary.

        Args:
            path: Directory path to ensure exists
            ctx: Migration context for logging

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.dry_run:
                self.logger.debug(f"      [DRY RUN] Would create directory: {path}")
                return True

            path.mkdir(parents=True, exist_ok=True)
            return True

        except Exception as e:
            error_msg = f"Failed to create directory {path}: {str(e)}"
            ctx.errors.append(error_msg)
            self.logger.error(error_msg)
            return False
