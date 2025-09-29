"""Utility functions for CRSBench builders.

This module provides helper functions for process execution, error handling,
and other common builder operations. The implementation is adapted from
PatchAgent's builder utilities (https://github.com/cla7aye15I4nd/PatchAgent)
under Apache 2.0 license.

Original PatchAgent citation:
    Yu, Zheng et al. "PatchAgent: A Practical Program Repair Agent Mimicking Human Expertise"
    34rd USENIX Security Symposium (USENIX Security 25), 2025.
"""

import subprocess
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class DockerUnavailableError(Exception):
    """Exception raised when Docker is not available or fails."""
    pass


class BuilderError(Exception):
    """Base exception for builder errors.

    Adapted from PatchAgent's BuilderError with enhanced error reporting.
    """

    def __init__(
        self,
        message: str,
        command: List[str],
        cwd: Path,
        stdout: str,
        stderr: str,
        return_code: Optional[int] = None
    ):
        self.message = message
        self.command = command
        self.cwd = cwd
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code

        # Create detailed error report
        report = f"""
        ===========Message===========
            {message}
        ===========Command===========
            {' '.join(command)}
        ===========CWD===========
            {cwd}
        ===========Return Code===========
            {return_code}
        ===========STDOUT===========
            {stdout}
        ===========STDERR===========
            {stderr}
        ===============================
        """

        super().__init__(report)


class BuilderProcessError(BuilderError):
    """Exception raised when a subprocess returns non-zero exit code."""
    pass


class BuilderTimeoutError(BuilderError):
    """Exception raised when a subprocess times out."""
    pass


def safe_subprocess_run(
    command: List[str],
    cwd: Path,
    input: Optional[bytes] = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, Any]] = None,
    capture_output: bool = True,
    check: bool = True
) -> bytes:
    """Run subprocess safely with comprehensive error handling.

    This function is adapted from PatchAgent's safe_subprocess_run with
    additional CRSBench-specific enhancements.

    Args:
        command: Command and arguments to execute
        cwd: Working directory for the command
        input: Optional input to send to the process
        timeout: Timeout in seconds
        env: Environment variables
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise exception on non-zero exit

    Returns:
        Process stdout as bytes

    Raises:
        BuilderProcessError: If process returns non-zero exit code
        BuilderTimeoutError: If process times out
    """
    logger.debug(f"Running command: {' '.join(command)} in {cwd}")

    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            input=input,
            capture_output=capture_output,
            text=False,
            check=check,
            timeout=timeout,
            env=env,
        )

        logger.debug(f"Command completed with return code: {process.returncode}")
        return process.stdout

    except subprocess.CalledProcessError as e:
        stdout = e.stdout.decode(errors="ignore") if e.stdout else ""
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""

        logger.error(f"Process failed with return code {e.returncode}: {' '.join(command)}")

        raise BuilderProcessError(
            message=f"Process failed with return code {e.returncode}",
            command=command,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            return_code=e.returncode
        )

    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode(errors="ignore") if e.stdout else ""
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""

        logger.error(f"Process timed out after {timeout} seconds: {' '.join(command)}")

        raise BuilderTimeoutError(
            message=f"Process timed out after {timeout} seconds",
            command=command,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr
        )


def check_docker_available() -> bool:
    """Check if Docker is available and running.

    Returns:
        True if Docker is available, False otherwise
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=True
        )
        logger.debug("Docker is available")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Docker is not available or not running")
        return False


def check_command_exists(command: str) -> bool:
    """Check if a command exists in the system PATH.

    Args:
        command: Command name to check

    Returns:
        True if command exists, False otherwise
    """
    try:
        subprocess.run(
            ["which", command],
            capture_output=True,
            check=True,
            timeout=5
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_git_commit_hash(repo_path: Path) -> Optional[str]:
    """Get the current git commit hash of a repository.

    Args:
        repo_path: Path to git repository

    Returns:
        Commit hash string or None if not a git repo
    """
    try:
        result = safe_subprocess_run(
            ["git", "rev-parse", "HEAD"],
            repo_path,
            timeout=10
        )
        return result.decode().strip()
    except Exception:
        return None


def create_temp_file(content: Union[str, bytes], suffix: str = "") -> Path:
    """Create a temporary file with given content.

    Args:
        content: Content to write to file
        suffix: Optional file suffix

    Returns:
        Path to created temporary file
    """
    import tempfile

    # Create temporary file
    fd, path = tempfile.mkstemp(suffix=suffix)

    try:
        if isinstance(content, str):
            content = content.encode()

        with open(fd, 'wb') as f:
            f.write(content)

        return Path(path)
    except Exception:
        # Clean up on error
        Path(path).unlink(missing_ok=True)
        raise


def parse_sanitizer_output(output: str) -> Dict[str, Any]:
    """Parse sanitizer output to extract relevant information.

    Args:
        output: Raw sanitizer output

    Returns:
        Dictionary with parsed information
    """
    result = {
        "has_error": False,
        "error_type": None,
        "error_location": None,
        "stack_trace": [],
        "summary": ""
    }

    lines = output.split('\n')

    # Look for common sanitizer patterns
    for i, line in enumerate(lines):
        line_lower = line.lower()

        # AddressSanitizer patterns
        if "addresssanitizer" in line_lower:
            result["has_error"] = True
            if "heap-buffer-overflow" in line_lower:
                result["error_type"] = "heap-buffer-overflow"
            elif "heap-use-after-free" in line_lower:
                result["error_type"] = "heap-use-after-free"
            elif "segv" in line_lower:
                result["error_type"] = "segmentation-fault"
            elif "stack-buffer-overflow" in line_lower:
                result["error_type"] = "stack-buffer-overflow"

        # MemorySanitizer patterns
        elif "memorysanitizer" in line_lower:
            result["has_error"] = True
            result["error_type"] = "uninitialized-memory"

        # UndefinedBehaviorSanitizer patterns
        elif "ubsan" in line_lower or "undefined behavior" in line_lower:
            result["has_error"] = True
            result["error_type"] = "undefined-behavior"

        # ThreadSanitizer patterns
        elif "threadsanitizer" in line_lower:
            result["has_error"] = True
            result["error_type"] = "data-race"

        # Extract location information
        if result["has_error"] and "#0" in line and not result["error_location"]:
            # First stack frame often contains the error location
            result["error_location"] = line.strip()

        # Collect stack trace
        if line.strip().startswith("#"):
            result["stack_trace"].append(line.strip())

    # Create summary
    if result["has_error"]:
        error_type = result["error_type"] or "unknown"
        result["summary"] = f"Sanitizer detected {error_type}"
        if result["error_location"]:
            result["summary"] += f" at {result['error_location']}"
    else:
        result["summary"] = "No sanitizer errors detected"

    return result


def validate_patch_format(patch: str) -> bool:
    """Validate that a patch is in proper unified diff format.

    Args:
        patch: Patch content to validate

    Returns:
        True if patch format is valid
    """
    if not patch.strip():
        return False

    lines = patch.split('\n')

    # Check for unified diff headers
    has_diff_header = False
    has_hunk_header = False

    for line in lines:
        if line.startswith('diff --git') or line.startswith('--- ') or line.startswith('+++ '):
            has_diff_header = True
        elif line.startswith('@@'):
            has_hunk_header = True

    return has_diff_header and has_hunk_header