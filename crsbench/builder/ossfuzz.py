"""OSS-Fuzz builder implementation for CRSBench.

This module provides OSS-Fuzz specific building and testing functionality.
The implementation is heavily adapted from PatchAgent's OSS-Fuzz builder
(https://github.com/cla7aye15I4nd/PatchAgent) under Apache 2.0 license,
with extensions for CRSBench's benchmark structure and POV validation.

Original PatchAgent citation:
    Yu, Zheng et al. "PatchAgent: A Practical Program Repair Agent Mimicking Human Expertise"
    34rd USENIX Security Symposium (USENIX Security 25), 2025.

Key adaptations for CRSBench:
- Integration with CRSBench POV format
- Support for .aixcc benchmark configuration
- Enhanced sanitizer report parsing
- Integration with existing reproducer/patch_tester modules
"""

import os
import shutil
import subprocess
import time
from functools import cached_property
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pexpect
import yaml

from crsbench.builder.base import Builder, BuildResult, BuildStatus, Language, Sanitizer
from crsbench.builder.poc import POC, POCType, POCMetadata
from crsbench.builder.utils import (
    BuilderProcessError,
    BuilderTimeoutError,
    DockerUnavailableError,
    safe_subprocess_run,
    check_docker_available,
    parse_sanitizer_output
)

import logging

logger = logging.getLogger(__name__)


class OSSFuzzPOC(POC):
    """OSS-Fuzz specific POC implementation.

    This class adapts PatchAgent's OSSFuzzPoC for CRSBench's POC interface.
    """

    def __init__(self, file_path: Path, harness_name: str, metadata: Optional[POCMetadata] = None):
        """Initialize OSS-Fuzz POC.

        Args:
            file_path: Path to POC file
            harness_name: Name of the target harness
            metadata: Optional metadata (will be created if not provided)
        """
        if metadata is None:
            metadata = POCMetadata(
                name=file_path.stem,
                poc_type=POCType.OSSFUZZ,
                target_harness=harness_name
            )

        super().__init__(metadata)
        self.file_path = Path(file_path)
        self.harness_name = harness_name

        if not self.file_path.exists():
            raise FileNotFoundError(f"OSS-Fuzz POC file not found: {file_path}")

    @property
    def data(self) -> bytes:
        """Get POC data from file."""
        return self.file_path.read_bytes()

    @property
    def poc_type(self) -> POCType:
        """Get POC type."""
        return POCType.OSSFUZZ

    @property
    def path(self) -> Path:
        """Get file path (compatibility with PatchAgent interface)."""
        return self.file_path


class OSSFuzzBuilder(Builder):
    """OSS-Fuzz project builder.

    This class provides building and testing functionality for OSS-Fuzz projects.
    The implementation is adapted from PatchAgent's OSSFuzzBuilder with extensions
    for CRSBench integration.
    """

    # Sanitizer mapping from CRSBench to OSS-Fuzz
    SANITIZER_MAP = {
        Sanitizer.AddressSanitizer: "address",
        Sanitizer.UndefinedBehaviorSanitizer: "undefined",
        Sanitizer.LeakAddressSanitizer: "address",
        Sanitizer.MemorySanitizer: "memory",
        Sanitizer.JazzerSanitizer: "address",  # OSS-Fuzz maps Jazzer to address for JVM
    }

    def __init__(
        self,
        project: str,
        source_path: Path,
        ossfuzz_path: Path,
        sanitizers: List[Sanitizer],
        workspace: Optional[Path] = None,
        clean_up: bool = True,
        timeout: int = 300,
        replay_timeout: int = 360
    ):
        """Initialize OSS-Fuzz builder.

        Args:
            project: OSS-Fuzz project name
            source_path: Path to project source code
            ossfuzz_path: Path to OSS-Fuzz repository
            sanitizers: List of sanitizers to use
            workspace: Working directory (optional)
            clean_up: Whether to clean workspace on init
            timeout: Default timeout for operations
            replay_timeout: Timeout for POC replay operations
        """
        super().__init__(project, source_path, workspace, clean_up, timeout)

        self.org_ossfuzz_path = Path(ossfuzz_path)
        self.sanitizers = sanitizers
        self.replay_timeout = replay_timeout

        # Verify OSS-Fuzz path
        if not self.org_ossfuzz_path.exists():
            raise FileNotFoundError(f"OSS-Fuzz path not found: {ossfuzz_path}")

        # Verify Docker is available
        if not check_docker_available():
            raise DockerUnavailableError("Docker is not available or not running")

        logger.info(f"Initialized OSS-Fuzz builder for project '{project}'")

    @cached_property
    def ossfuzz_path(self) -> Path:
        """Get immutable copy of OSS-Fuzz path in workspace."""
        target_path = self.workspace / "immutable" / self.org_ossfuzz_path.name
        if not target_path.is_dir():
            shutil.copytree(self.org_ossfuzz_path, target_path, symlinks=True)
        return target_path

    @property
    def language(self) -> Language:
        """Get project language from project.yaml."""
        project_yaml = self.ossfuzz_path / "projects" / self.project / "project.yaml"
        if not project_yaml.exists():
            logger.warning(f"project.yaml not found for {self.project}, defaulting to C")
            return Language.C

        try:
            with open(project_yaml) as f:
                yaml_data = yaml.safe_load(f)
            lang_str = yaml_data.get("language", "c")
            return Language.from_str(lang_str)
        except Exception as e:
            logger.warning(f"Failed to parse project.yaml: {e}, defaulting to C")
            return Language.C

    @property
    def supported_sanitizers(self) -> List[Sanitizer]:
        """Get supported sanitizers for this builder."""
        return list(self.SANITIZER_MAP.keys())

    def hash_patch(self, sanitizer: Sanitizer, patch: str) -> str:
        """Create hash for patch and sanitizer combination."""
        return f"{md5(patch.encode()).hexdigest()}-{self.SANITIZER_MAP[sanitizer]}"

    def build_finish_indicator(self, sanitizer: Sanitizer, patch: str) -> Path:
        """Get path to build completion indicator file."""
        return self.workspace / self.hash_patch(sanitizer, patch) / ".build"

    def _build_image(self, ossfuzz_path: Path, tries: int = 3) -> None:
        """Build Docker image for the project.

        Args:
            ossfuzz_path: Path to OSS-Fuzz repository
            tries: Number of retry attempts
        """
        for attempt in range(tries):
            logger.debug(f"Building Docker image (attempt {attempt + 1}/{tries})")

            try:
                process = subprocess.Popen(
                    ["infra/helper.py", "build_image", "--pull", self.project],
                    cwd=ossfuzz_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                stdout, stderr = process.communicate()
                if process.returncode == 0:
                    logger.debug("Docker image built successfully")
                    return

                logger.warning(f"Docker image build failed (attempt {attempt + 1}): {stderr.decode()}")

            except Exception as e:
                logger.warning(f"Docker image build error (attempt {attempt + 1}): {e}")

        # All attempts failed
        raise DockerUnavailableError(f"Failed to build Docker image after {tries} attempts")

    def _build_project(self, sanitizer: Sanitizer, patch: str = "") -> None:
        """Build the project with given sanitizer and patch.

        Args:
            sanitizer: Sanitizer to use
            patch: Optional patch to apply
        """
        if self.build_finish_indicator(sanitizer, patch).is_file():
            logger.debug(f"Build already completed for {self.hash_patch(sanitizer, patch)}")
            return

        build_hash = self.hash_patch(sanitizer, patch)
        logger.info(f"Building {self.project} with patch {build_hash}")

        # Create workspace for this build
        workspace = self.workspace / build_hash
        source_path = workspace / self.org_source_path.name
        ossfuzz_path = workspace / self.org_ossfuzz_path.name

        # Clean and recreate workspace
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(self.source_path, source_path, symlinks=True)
        shutil.copytree(self.ossfuzz_path, ossfuzz_path, symlinks=True)

        # Apply patch if provided
        if patch.strip():
            logger.debug("Applying patch")
            safe_subprocess_run(
                ["patch", "-p1"],
                source_path,
                input=patch.encode(),
                timeout=60
            )

        # Build Docker image
        self._build_image(ossfuzz_path)

        # Build fuzzers
        logger.debug("Building fuzzers")
        safe_subprocess_run(
            [
                "infra/helper.py",
                "build_fuzzers",
                "--sanitizer",
                self.SANITIZER_MAP[sanitizer],
                "--clean",
                self.project,
                source_path,
            ],
            ossfuzz_path,
            timeout=self.timeout
        )

        # Check build
        logger.debug("Checking build")
        safe_subprocess_run(
            [
                "infra/helper.py",
                "check_build",
                "--sanitizer",
                self.SANITIZER_MAP[sanitizer],
                self.project,
            ],
            ossfuzz_path,
            timeout=60
        )

        # Mark build as completed
        self.build_finish_indicator(sanitizer, patch).write_text(patch)
        logger.info(f"Build completed for {build_hash}")

    def build(self, patch: str = "", sanitizer: Optional[Sanitizer] = None) -> BuildResult:
        """Build the project.

        Args:
            patch: Optional patch to apply
            sanitizer: Optional specific sanitizer to use

        Returns:
            BuildResult with build status
        """
        start_time = time.time()
        output = []
        error_output = []

        try:
            sanitizers_to_build = [sanitizer] if sanitizer else self.sanitizers

            for san in sanitizers_to_build:
                logger.info(f"Building with {san.value} sanitizer")
                self._build_project(san, patch)
                output.append(f"Build successful with {san.value} sanitizer")

            execution_time = time.time() - start_time
            return BuildResult(
                status=BuildStatus.SUCCESS,
                output="\n".join(output),
                execution_time=execution_time
            )

        except BuilderTimeoutError as e:
            execution_time = time.time() - start_time
            return BuildResult(
                status=BuildStatus.TIMEOUT,
                output=e.stdout,
                error_output=e.stderr,
                execution_time=execution_time
            )

        except BuilderProcessError as e:
            execution_time = time.time() - start_time
            return BuildResult(
                status=BuildStatus.FAILED,
                output=e.stdout,
                error_output=e.stderr,
                execution_time=execution_time
            )

        except Exception as e:
            execution_time = time.time() - start_time
            return BuildResult(
                status=BuildStatus.ERROR,
                error_output=str(e),
                execution_time=execution_time
            )

    def _replay_poc(self, poc: OSSFuzzPOC, sanitizer: Sanitizer, patch: str = "") -> Dict[str, Any]:
        """Replay a POC with given sanitizer and patch.

        Args:
            poc: OSS-Fuzz POC to replay
            sanitizer: Sanitizer to use
            patch: Optional patch to apply

        Returns:
            Dictionary with replay results
        """
        # Ensure build is complete
        self._build_project(sanitizer, patch)

        build_hash = self.hash_patch(sanitizer, patch)
        if not self.build_finish_indicator(sanitizer, patch).is_file():
            raise BuilderProcessError(
                message="Build not completed",
                command=[],
                cwd=self.workspace,
                stdout="",
                stderr="Build indicator file not found"
            )

        logger.info(f"Replaying POC {poc.name} for {poc.harness_name} with {build_hash}")

        ossfuzz_workspace = self.workspace / build_hash / self.org_ossfuzz_path.name

        try:
            # Run OSS-Fuzz reproduce command
            result = safe_subprocess_run(
                [
                    "infra/helper.py",
                    "reproduce",
                    self.project,
                    poc.harness_name,
                    poc.path,
                ],
                ossfuzz_workspace,
                timeout=self.replay_timeout,
            )

            # If we get here, no crash occurred
            return {
                "triggered": False,
                "output": result.decode(errors="ignore"),
                "error_output": "",
                "sanitizer_report": None,
                "summary": "POC completed without triggering vulnerability"
            }

        except BuilderProcessError as e:
            # Parse the error output for sanitizer reports
            combined_output = f"{e.stdout}\n{e.stderr}"
            sanitizer_info = parse_sanitizer_output(combined_output)

            # Check for Docker errors
            if "docker: Error response from daemon:" in combined_output:
                raise DockerUnavailableError(combined_output)

            return {
                "triggered": sanitizer_info["has_error"],
                "output": e.stdout,
                "error_output": e.stderr,
                "sanitizer_report": sanitizer_info,
                "summary": sanitizer_info["summary"]
            }

    def test_pov(self, poc_data: bytes, harness_name: str,
                 patch: str = "", sanitizer: Optional[Sanitizer] = None) -> Dict[str, Any]:
        """Test a Proof of Vulnerability.

        Args:
            poc_data: POV input data
            harness_name: Name of the harness to test
            patch: Optional patch to apply before testing
            sanitizer: Sanitizer to use for testing

        Returns:
            Dictionary with test results
        """
        # Create temporary POC file
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".poc") as f:
            f.write(poc_data)
            temp_poc_path = Path(f.name)

        try:
            # Create OSS-Fuzz POC
            poc = OSSFuzzPOC(temp_poc_path, harness_name)

            # Test with specified sanitizer or try all sanitizers
            sanitizers_to_test = [sanitizer] if sanitizer else self.sanitizers

            for san in sanitizers_to_test:
                result = self._replay_poc(poc, san, patch)
                if result["triggered"]:
                    return result

            # No sanitizer triggered
            return {
                "triggered": False,
                "output": "POC tested with all sanitizers",
                "error_output": "",
                "sanitizer_report": None,
                "summary": "POC did not trigger any vulnerabilities"
            }

        finally:
            # Clean up temporary file
            temp_poc_path.unlink(missing_ok=True)

    def replay_poc_file(self, poc_file: Path, harness_name: str,
                       patch: str = "", sanitizer: Optional[Sanitizer] = None) -> Dict[str, Any]:
        """Replay a POC from file.

        Args:
            poc_file: Path to POC file
            harness_name: Name of the harness to test
            patch: Optional patch to apply before testing
            sanitizer: Sanitizer to use for testing

        Returns:
            Dictionary with test results
        """
        poc = OSSFuzzPOC(poc_file, harness_name)

        # Test with specified sanitizer or try all sanitizers
        sanitizers_to_test = [sanitizer] if sanitizer else self.sanitizers

        for san in sanitizers_to_test:
            result = self._replay_poc(poc, san, patch)
            if result["triggered"]:
                return result

        # No sanitizer triggered
        return {
            "triggered": False,
            "output": "POC tested with all sanitizers",
            "error_output": "",
            "sanitizer_report": None,
            "summary": "POC did not trigger any vulnerabilities"
        }

    def function_test(self, patch: str = "") -> BuildResult:
        """Run functional tests (OSS-Fuzz doesn't typically have function tests)."""
        logger.info("OSS-Fuzz projects typically don't have dedicated function tests")
        return BuildResult(status=BuildStatus.SUCCESS, output="No function tests to run")

    def cleanup(self):
        """Clean up workspace and Docker resources."""
        super().cleanup()

        # Could add Docker cleanup here if needed
        logger.debug("OSS-Fuzz builder cleanup completed")