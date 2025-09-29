"""Patch application functionality."""

import os
import subprocess
import tempfile
import logging
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ApplicationStatus(Enum):
    """Status of patch application."""
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    REJECTED = "rejected"
    MALFORMED = "malformed"


@dataclass
class PatchApplication:
    """Result of patch application."""
    patch_name: str
    status: ApplicationStatus
    applied_files: List[str]
    failed_files: List[str]
    rejected_hunks: List[str]
    output: str
    error_message: Optional[str] = None


class PatchApplicator:
    """Applies patches to codebase using various methods."""

    def __init__(self,
                 dry_run: bool = False,
                 strip_level: int = 1,
                 fuzz_factor: int = 2):
        """Initialize patch applicator.

        Args:
            dry_run: If True, don't actually apply patches (test only)
            strip_level: Number of leading path components to strip
            fuzz_factor: Fuzz factor for patch application
        """
        self.dry_run = dry_run
        self.strip_level = strip_level
        self.fuzz_factor = fuzz_factor

    def apply_patch(self,
                   benchmark_path: Path,
                   patch_content: str,
                   patch_name: str) -> PatchApplication:
        """Apply a patch to the benchmark codebase.

        Args:
            benchmark_path: Path to benchmark directory
            patch_content: Patch content in unified diff format
            patch_name: Name/identifier for the patch

        Returns:
            PatchApplication result
        """
        logger.info(f"Applying patch '{patch_name}' to {benchmark_path}")

        # First try to apply with system patch command
        try:
            return self._apply_with_patch_command(benchmark_path, patch_content, patch_name)
        except Exception as e:
            logger.warning(f"System patch command failed: {e}")

        # Fallback to git apply
        try:
            return self._apply_with_git(benchmark_path, patch_content, patch_name)
        except Exception as e:
            logger.warning(f"Git apply failed: {e}")

        # Fallback to manual application
        try:
            return self._apply_manually(benchmark_path, patch_content, patch_name)
        except Exception as e:
            logger.error(f"Manual patch application failed: {e}")

        return PatchApplication(
            patch_name=patch_name,
            status=ApplicationStatus.FAILED,
            applied_files=[],
            failed_files=[],
            rejected_hunks=[],
            output="",
            error_message="All patch application methods failed"
        )

    def _apply_with_patch_command(self,
                                benchmark_path: Path,
                                patch_content: str,
                                patch_name: str) -> PatchApplication:
        """Apply patch using system patch command."""
        logger.debug("Attempting patch application with system 'patch' command")

        # Write patch to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as f:
            f.write(patch_content)
            patch_file = Path(f.name)

        try:
            # Build patch command
            cmd = [
                'patch',
                f'-p{self.strip_level}',
                f'--fuzz={self.fuzz_factor}',
                '--no-backup-if-mismatch',
                '--verbose'
            ]

            if self.dry_run:
                cmd.append('--dry-run')

            # Apply patch
            result = subprocess.run(
                cmd,
                input=patch_content,
                cwd=benchmark_path,
                capture_output=True,
                text=True,
                timeout=60
            )

            # Parse output
            applied_files = self._extract_applied_files(result.stdout)
            failed_files = self._extract_failed_files(result.stderr)
            rejected_hunks = self._extract_rejected_hunks(result.stderr)

            if result.returncode == 0:
                status = ApplicationStatus.SUCCESS
            elif applied_files and failed_files:
                status = ApplicationStatus.PARTIAL
            else:
                status = ApplicationStatus.FAILED

            return PatchApplication(
                patch_name=patch_name,
                status=status,
                applied_files=applied_files,
                failed_files=failed_files,
                rejected_hunks=rejected_hunks,
                output=result.stdout + result.stderr,
                error_message=result.stderr if result.returncode != 0 else None
            )

        except subprocess.TimeoutExpired:
            return PatchApplication(
                patch_name=patch_name,
                status=ApplicationStatus.FAILED,
                applied_files=[],
                failed_files=[],
                rejected_hunks=[],
                output="",
                error_message="Patch application timed out"
            )
        except Exception as e:
            raise Exception(f"Patch command failed: {e}")
        finally:
            # Clean up temporary file
            if patch_file.exists():
                patch_file.unlink()

    def _apply_with_git(self,
                       benchmark_path: Path,
                       patch_content: str,
                       patch_name: str) -> PatchApplication:
        """Apply patch using git apply."""
        logger.debug("Attempting patch application with 'git apply'")

        # Build git apply command
        cmd = [
            'git', 'apply',
            '--verbose',
            '--ignore-whitespace',
            f'--p{self.strip_level}'
        ]

        if self.dry_run:
            cmd.append('--check')

        try:
            result = subprocess.run(
                cmd,
                input=patch_content,
                cwd=benchmark_path,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                # For git apply, we need to parse the patch to determine affected files
                applied_files = self._extract_files_from_patch(patch_content)
                return PatchApplication(
                    patch_name=patch_name,
                    status=ApplicationStatus.SUCCESS,
                    applied_files=applied_files,
                    failed_files=[],
                    rejected_hunks=[],
                    output=result.stdout + result.stderr
                )
            else:
                return PatchApplication(
                    patch_name=patch_name,
                    status=ApplicationStatus.FAILED,
                    applied_files=[],
                    failed_files=self._extract_files_from_patch(patch_content),
                    rejected_hunks=[],
                    output=result.stdout + result.stderr,
                    error_message=result.stderr
                )

        except subprocess.TimeoutExpired:
            return PatchApplication(
                patch_name=patch_name,
                status=ApplicationStatus.FAILED,
                applied_files=[],
                failed_files=[],
                rejected_hunks=[],
                output="",
                error_message="Git apply timed out"
            )
        except Exception as e:
            raise Exception(f"Git apply failed: {e}")

    def _apply_manually(self,
                       benchmark_path: Path,
                       patch_content: str,
                       patch_name: str) -> PatchApplication:
        """Apply patch manually by parsing and applying hunks."""
        logger.debug("Attempting manual patch application")

        try:
            # Parse patch content
            patch_files = self._parse_patch_content(patch_content)
            applied_files = []
            failed_files = []

            for file_path, hunks in patch_files.items():
                try:
                    if self._apply_hunks_to_file(benchmark_path / file_path, hunks):
                        applied_files.append(file_path)
                    else:
                        failed_files.append(file_path)
                except Exception as e:
                    logger.error(f"Failed to apply hunks to {file_path}: {e}")
                    failed_files.append(file_path)

            if applied_files and not failed_files:
                status = ApplicationStatus.SUCCESS
            elif applied_files and failed_files:
                status = ApplicationStatus.PARTIAL
            else:
                status = ApplicationStatus.FAILED

            return PatchApplication(
                patch_name=patch_name,
                status=status,
                applied_files=applied_files,
                failed_files=failed_files,
                rejected_hunks=[],
                output=f"Manually applied to {len(applied_files)} files"
            )

        except Exception as e:
            raise Exception(f"Manual patch application failed: {e}")

    def _extract_applied_files(self, output: str) -> List[str]:
        """Extract list of successfully applied files from patch output."""
        applied_files = []
        for line in output.split('\n'):
            if 'patching file' in line.lower():
                parts = line.split()
                if len(parts) >= 3:
                    applied_files.append(parts[2])
        return applied_files

    def _extract_failed_files(self, error_output: str) -> List[str]:
        """Extract list of failed files from patch error output."""
        failed_files = []
        for line in error_output.split('\n'):
            if 'failed' in line.lower() and 'file' in line.lower():
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.lower() == 'file' and i + 1 < len(parts):
                        failed_files.append(parts[i + 1])
        return failed_files

    def _extract_rejected_hunks(self, error_output: str) -> List[str]:
        """Extract list of rejected hunks from patch error output."""
        rejected_hunks = []
        for line in error_output.split('\n'):
            if 'rejected' in line.lower() and 'hunk' in line.lower():
                rejected_hunks.append(line.strip())
        return rejected_hunks

    def _extract_files_from_patch(self, patch_content: str) -> List[str]:
        """Extract file paths from patch content."""
        files = []
        for line in patch_content.split('\n'):
            if line.startswith('+++') or line.startswith('---'):
                parts = line.split('\t')[0].split()
                if len(parts) >= 2:
                    file_path = parts[1]
                    # Remove common prefixes
                    if file_path.startswith('a/') or file_path.startswith('b/'):
                        file_path = file_path[2:]
                    if file_path not in files and file_path != '/dev/null':
                        files.append(file_path)
        return files

    def _parse_patch_content(self, patch_content: str) -> dict:
        """Parse patch content into file->hunks mapping."""
        # This is a simplified parser - in practice, you'd want a more robust implementation
        files = {}
        current_file = None
        current_hunk = []

        for line in patch_content.split('\n'):
            if line.startswith('+++'):
                # New file
                parts = line.split('\t')[0].split()
                if len(parts) >= 2:
                    file_path = parts[1]
                    if file_path.startswith('b/'):
                        file_path = file_path[2:]
                    current_file = file_path
                    files[current_file] = []
            elif line.startswith('@@'):
                # New hunk
                if current_hunk and current_file:
                    files[current_file].append(current_hunk)
                current_hunk = [line]
            elif current_hunk is not None:
                current_hunk.append(line)

        # Add last hunk
        if current_hunk and current_file:
            files[current_file].append(current_hunk)

        return files

    def _apply_hunks_to_file(self, file_path: Path, hunks: List[List[str]]) -> bool:
        """Apply hunks to a specific file."""
        # This is a placeholder for manual hunk application
        # In practice, this would involve:
        # 1. Reading the file
        # 2. Parsing hunk headers to determine line ranges
        # 3. Applying additions/deletions
        # 4. Writing the modified file back

        logger.warning(f"Manual hunk application not fully implemented for {file_path}")
        return False  # Return False for now to indicate failure