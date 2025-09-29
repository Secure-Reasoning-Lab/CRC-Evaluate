"""Git state management for patch testing."""

import logging
import subprocess
from enum import Enum
from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)


class GitOperation(Enum):
    """Git operations performed during patch testing."""
    CREATE_BRANCH = "create_branch"
    SWITCH_BRANCH = "switch_branch"
    STASH_CHANGES = "stash_changes"
    RESTORE_CHANGES = "restore_changes"
    COMMIT_CHANGES = "commit_changes"
    RESET_HARD = "reset_hard"


@dataclass
class GitState:
    """Represents git repository state."""
    current_branch: str
    is_dirty: bool
    stash_created: bool = False
    temp_branch_created: Optional[str] = None
    original_commit: Optional[str] = None


class GitManager:
    """Manages git state during patch testing to ensure clean restoration."""

    def __init__(self, timeout: int = 30):
        """Initialize git manager.

        Args:
            timeout: Timeout for git operations in seconds
        """
        self.timeout = timeout
        self.saved_states = {}  # repo_path -> GitState

    def save_state(self, repo_path: Path) -> GitState:
        """Save current git state for later restoration.

        Args:
            repo_path: Path to git repository

        Returns:
            GitState object representing current state
        """
        logger.debug(f"Saving git state for {repo_path}")

        if not self._is_git_repo(repo_path):
            raise ValueError(f"Not a git repository: {repo_path}")

        # Get current branch
        current_branch = self._get_current_branch(repo_path)
        if current_branch is None:
            raise ValueError("Unable to determine current branch")

        # Check if repository is dirty
        is_dirty = self._is_repo_dirty(repo_path)

        # Get current commit
        original_commit = self._get_current_commit(repo_path)

        # Create state object
        state = GitState(
            current_branch=current_branch,
            is_dirty=is_dirty,
            original_commit=original_commit
        )

        # Save state
        self.saved_states[str(repo_path)] = state

        logger.info(f"Git state saved: branch={current_branch}, dirty={is_dirty}")
        return state

    def create_branch(self, repo_path: Path, branch_name: str) -> bool:
        """Create and switch to a new branch.

        Args:
            repo_path: Path to git repository
            branch_name: Name of new branch to create

        Returns:
            True if successful, False otherwise
        """
        logger.debug(f"Creating branch '{branch_name}' in {repo_path}")

        # Save state if not already saved
        if str(repo_path) not in self.saved_states:
            self.save_state(repo_path)

        state = self.saved_states[str(repo_path)]

        try:
            # Stash changes if repository is dirty
            if state.is_dirty and not state.stash_created:
                if not self._stash_changes(repo_path):
                    logger.error("Failed to stash changes")
                    return False
                state.stash_created = True

            # Create and switch to new branch
            if not self._create_and_switch_branch(repo_path, branch_name):
                logger.error(f"Failed to create branch '{branch_name}'")
                return False

            state.temp_branch_created = branch_name
            logger.info(f"Successfully created and switched to branch '{branch_name}'")
            return True

        except Exception as e:
            logger.error(f"Error creating branch '{branch_name}': {e}")
            return False

    def restore_original_state(self, repo_path: Path) -> bool:
        """Restore repository to its original state.

        Args:
            repo_path: Path to git repository

        Returns:
            True if successful, False otherwise
        """
        logger.debug(f"Restoring original git state for {repo_path}")

        state = self.saved_states.get(str(repo_path))
        if not state:
            logger.warning("No saved state found - nothing to restore")
            return True

        try:
            # Switch back to original branch
            if not self._switch_branch(repo_path, state.current_branch):
                logger.error(f"Failed to switch back to '{state.current_branch}'")
                return False

            # Delete temporary branch if created
            if state.temp_branch_created:
                if not self._delete_branch(repo_path, state.temp_branch_created):
                    logger.warning(f"Failed to delete temporary branch '{state.temp_branch_created}'")

            # Restore stashed changes if any
            if state.stash_created:
                if not self._restore_stash(repo_path):
                    logger.error("Failed to restore stashed changes")
                    return False

            # Clean up saved state
            del self.saved_states[str(repo_path)]

            logger.info("Git state successfully restored")
            return True

        except Exception as e:
            logger.error(f"Error restoring git state: {e}")
            return False

    def commit_changes(self, repo_path: Path, message: str) -> bool:
        """Commit current changes.

        Args:
            repo_path: Path to git repository
            message: Commit message

        Returns:
            True if successful, False otherwise
        """
        logger.debug(f"Committing changes with message: {message}")

        try:
            # Add all changes
            result = self._run_git_command(repo_path, ['add', '.'])
            if result.returncode != 0:
                logger.error("Failed to add changes")
                return False

            # Commit changes
            result = self._run_git_command(repo_path, ['commit', '-m', message])
            if result.returncode != 0:
                logger.error("Failed to commit changes")
                return False

            logger.info("Changes successfully committed")
            return True

        except Exception as e:
            logger.error(f"Error committing changes: {e}")
            return False

    def _is_git_repo(self, repo_path: Path) -> bool:
        """Check if directory is a git repository."""
        try:
            result = self._run_git_command(repo_path, ['rev-parse', '--git-dir'])
            return result.returncode == 0
        except Exception:
            return False

    def _get_current_branch(self, repo_path: Path) -> Optional[str]:
        """Get current branch name."""
        try:
            result = self._run_git_command(repo_path, ['branch', '--show-current'])
            if result.returncode == 0:
                return result.stdout.strip()

            # Fallback for older git versions
            result = self._run_git_command(repo_path, ['rev-parse', '--abbrev-ref', 'HEAD'])
            if result.returncode == 0:
                branch = result.stdout.strip()
                return branch if branch != 'HEAD' else None

            return None
        except Exception:
            return None

    def _get_current_commit(self, repo_path: Path) -> Optional[str]:
        """Get current commit hash."""
        try:
            result = self._run_git_command(repo_path, ['rev-parse', 'HEAD'])
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception:
            return None

    def _is_repo_dirty(self, repo_path: Path) -> bool:
        """Check if repository has uncommitted changes."""
        try:
            result = self._run_git_command(repo_path, ['status', '--porcelain'])
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    def _stash_changes(self, repo_path: Path) -> bool:
        """Stash current changes."""
        try:
            result = self._run_git_command(repo_path, ['stash', 'push', '-m', 'CRSBench patch testing stash'])
            return result.returncode == 0
        except Exception:
            return False

    def _restore_stash(self, repo_path: Path) -> bool:
        """Restore most recent stash."""
        try:
            result = self._run_git_command(repo_path, ['stash', 'pop'])
            return result.returncode == 0
        except Exception:
            return False

    def _create_and_switch_branch(self, repo_path: Path, branch_name: str) -> bool:
        """Create and switch to a new branch."""
        try:
            result = self._run_git_command(repo_path, ['checkout', '-b', branch_name])
            return result.returncode == 0
        except Exception:
            return False

    def _switch_branch(self, repo_path: Path, branch_name: str) -> bool:
        """Switch to an existing branch."""
        try:
            result = self._run_git_command(repo_path, ['checkout', branch_name])
            return result.returncode == 0
        except Exception:
            return False

    def _delete_branch(self, repo_path: Path, branch_name: str) -> bool:
        """Delete a branch."""
        try:
            result = self._run_git_command(repo_path, ['branch', '-D', branch_name])
            return result.returncode == 0
        except Exception:
            return False

    def _run_git_command(self, repo_path: Path, args: List[str]) -> subprocess.CompletedProcess:
        """Run a git command in the specified repository.

        Args:
            repo_path: Path to git repository
            args: Git command arguments

        Returns:
            CompletedProcess result
        """
        cmd = ['git'] + args
        return subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=self.timeout
        )

    def get_modified_files(self, repo_path: Path, since_commit: Optional[str] = None) -> List[str]:
        """Get list of modified files.

        Args:
            repo_path: Path to git repository
            since_commit: Compare against this commit (default: HEAD)

        Returns:
            List of modified file paths
        """
        try:
            if since_commit:
                result = self._run_git_command(repo_path, ['diff', '--name-only', since_commit])
            else:
                result = self._run_git_command(repo_path, ['diff', '--name-only', 'HEAD'])

            if result.returncode == 0:
                return [f.strip() for f in result.stdout.split('\n') if f.strip()]
            return []
        except Exception:
            return []

    def get_diff(self, repo_path: Path, since_commit: Optional[str] = None) -> str:
        """Get diff of changes.

        Args:
            repo_path: Path to git repository
            since_commit: Compare against this commit (default: HEAD)

        Returns:
            Diff output as string
        """
        try:
            if since_commit:
                result = self._run_git_command(repo_path, ['diff', since_commit])
            else:
                result = self._run_git_command(repo_path, ['diff', 'HEAD'])

            if result.returncode == 0:
                return result.stdout
            return ""
        except Exception:
            return ""