"""Base builder classes for CRSBench.

This module provides the abstract base classes for building and testing projects.
The design is inspired by and adapted from PatchAgent's builder architecture
(https://github.com/cla7aye15I4nd/PatchAgent) under Apache 2.0 license.

Original PatchAgent authors:
    Zheng Yu, Ziyi Guo, Yuhang Wu, Jiahao Yu, Meng Xu, Dongliang Mu, Yan Chen, Xinyu Xing
"""

import shutil
import tempfile
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Optional, List, Union, Dict, Any
from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging

from git import Repo

logger = logging.getLogger(__name__)


class BuildStatus(Enum):
    """Status of build operation."""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class Language(Enum):
    """Supported programming languages."""
    C = "c"
    CPP = "c++"
    CLIKE = "c/c++"
    JAVA = "java"
    JVM = "jvm"
    RUST = "rust"
    GO = "go"
    PYTHON = "python"
    JAVASCRIPT = "javascript"

    @classmethod
    def from_str(cls, lang_str: str) -> "Language":
        """Convert string to Language enum."""
        lang_lower = lang_str.lower()

        # Handle common variations
        if lang_lower in ["c", "c99", "c11"]:
            return cls.C
        elif lang_lower in ["c++", "cpp", "cxx", "cc"]:
            return cls.CPP
        elif lang_lower in ["c/c++", "clike"]:
            return cls.CLIKE
        elif lang_lower in ["java", "jvm"]:
            return cls.JAVA
        elif lang_lower in ["rust", "rs"]:
            return cls.RUST
        elif lang_lower in ["go", "golang"]:
            return cls.GO
        elif lang_lower in ["python", "py"]:
            return cls.PYTHON
        elif lang_lower in ["javascript", "js", "node"]:
            return cls.JAVASCRIPT
        else:
            # Default to C for unknown languages
            logger.warning(f"Unknown language '{lang_str}', defaulting to C")
            return cls.C


class Sanitizer(Enum):
    """Supported sanitizers."""
    AddressSanitizer = "address"
    MemorySanitizer = "memory"
    UndefinedBehaviorSanitizer = "undefined"
    ThreadSanitizer = "thread"
    LeakAddressSanitizer = "leak"
    JazzerSanitizer = "jazzer"
    JavaNativeSanitizer = "java_native"
    LibFuzzer = "libfuzzer"


@dataclass
class BuildResult:
    """Result of a build operation."""
    status: BuildStatus
    output: str = ""
    error_output: str = ""
    execution_time: float = 0.0
    artifacts: List[Path] = None

    def __post_init__(self):
        if self.artifacts is None:
            self.artifacts = []


class Builder(ABC):
    """Abstract base class for project builders.

    This class provides the interface and common functionality for building
    and testing projects. Specific implementations should inherit from this
    class and implement the abstract methods.

    Architecture adapted from PatchAgent's Builder class with CRSBench-specific
    extensions for POV validation and benchmark integration.
    """

    def __init__(
        self,
        project: str,
        source_path: Path,
        workspace: Optional[Path] = None,
        clean_up: bool = True,
        timeout: int = 300
    ):
        """Initialize builder.

        Args:
            project: Project name/identifier
            source_path: Path to project source code
            workspace: Working directory for builds (optional)
            clean_up: Whether to clean workspace on initialization
            timeout: Default timeout for operations in seconds
        """
        self.project = project
        self.org_source_path = source_path
        self.workspace = workspace or Path(tempfile.mkdtemp())
        self.timeout = timeout

        if clean_up:
            shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initialized builder for project '{project}' with workspace: {self.workspace}")

    @cached_property
    def source_path(self) -> Path:
        """Get immutable copy of source path in workspace."""
        target_path = self.workspace / "immutable" / self.org_source_path.name
        if not target_path.is_dir():
            shutil.copytree(self.org_source_path, target_path, symlinks=True)
        return target_path

    @cached_property
    def source_repo(self) -> Repo:
        """Get git repository for source code.

        Creates a git repository in the workspace for patch management.
        Adapted from PatchAgent's source_repo implementation.
        """
        target_path = self.workspace / "git" / self.org_source_path.name
        if not target_path.is_dir():
            shutil.copytree(self.source_path, target_path, symlinks=True)

        if (target_path / ".git").is_dir():
            shutil.rmtree(target_path / ".git")

        repo = Repo.init(target_path)

        # Add all files and create initial commit
        repo.git.add(repo.untracked_files)
        repo.index.commit("Initial commit")
        return repo

    @property
    @abstractmethod
    def language(self) -> Language:
        """Get the programming language of the project."""
        pass

    @property
    @abstractmethod
    def supported_sanitizers(self) -> List[Sanitizer]:
        """Get list of supported sanitizers for this builder."""
        pass

    def check_patch(self, patch: str) -> bool:
        """Check if a patch can be applied.

        Args:
            patch: Git patch content

        Returns:
            True if patch is valid and can be applied
        """
        logger.debug("Checking patch validity")

        try:
            self.source_repo.git.reset("--hard")
            self.source_repo.git.clean("-fdx")

            # Try to apply the patch
            from crsbench.builder.utils import safe_subprocess_run
            safe_subprocess_run(
                ["git", "apply", "--check"],
                Path(self.source_repo.working_dir),
                input=patch.encode(),
                timeout=30
            )
            return True
        except Exception as e:
            logger.warning(f"Patch check failed: {e}")
            return False

    def format_patch(self, patch: str) -> Optional[str]:
        """Format a patch to git diff format.

        Args:
            patch: Patch content

        Returns:
            Formatted git diff, or None if formatting failed
        """
        logger.debug("Formatting patch")

        try:
            self.source_repo.git.reset("--hard")
            self.source_repo.git.clean("-fdx")

            from crsbench.builder.utils import safe_subprocess_run

            # Apply patch
            safe_subprocess_run(
                ["patch", "-F", "3", "--no-backup-if-mismatch", "-p1"],
                Path(self.source_repo.working_dir),
                input=patch.encode(),
                timeout=60
            )

            # Get git diff
            result = safe_subprocess_run(
                ["git", "diff"],
                Path(self.source_repo.working_dir),
                timeout=30
            )

            return result.decode(errors="ignore")
        except Exception as e:
            logger.error(f"Patch formatting failed: {e}")
            return None

    @abstractmethod
    def build(self, patch: str = "", sanitizer: Optional[Sanitizer] = None) -> BuildResult:
        """Build the project.

        Args:
            patch: Optional patch to apply before building
            sanitizer: Sanitizer to use for the build

        Returns:
            BuildResult with build status and details
        """
        pass

    @abstractmethod
    def test_pov(self, poc_data: bytes, harness_name: str,
                 patch: str = "", sanitizer: Optional[Sanitizer] = None) -> Dict[str, Any]:
        """Test a Proof of Vulnerability.

        Args:
            poc_data: POV input data
            harness_name: Name of the harness to test
            patch: Optional patch to apply before testing
            sanitizer: Sanitizer to use for testing

        Returns:
            Dictionary with test results including:
            - triggered: bool indicating if vulnerability was triggered
            - output: execution output
            - error_output: error output
            - sanitizer_report: parsed sanitizer report if available
        """
        pass

    def function_test(self, patch: str = "") -> BuildResult:
        """Run functional tests for the project.

        Args:
            patch: Optional patch to apply before testing

        Returns:
            BuildResult with test status
        """
        # Default implementation - no functional tests
        logger.info("No functional tests defined for this builder")
        return BuildResult(status=BuildStatus.SUCCESS)

    def cleanup(self):
        """Clean up workspace and temporary files."""
        if self.workspace.exists():
            logger.info(f"Cleaning up workspace: {self.workspace}")
            shutil.rmtree(self.workspace, ignore_errors=True)

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.cleanup()