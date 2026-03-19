"""Shared helper functions for running OSS-Fuzz commands and benchmark tests.

This module provides utilities for:
- Running shell commands with proper error handling
- Building benchmarks using OSS-Fuzz infrastructure
- Running test.sh inside Docker containers
- Applying and reverting patches
- Reproducing POVs

Used by:
- crsbench.benchmark_ci: For CI testing
- crsbench.migration: For test.sh generation (MCP server)
"""

import os
import re
import shutil
import signal
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple, Union, cast

from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import (
    ensure_project_repository,
    get_repo_info_from_benchmark,
    run_git,
)

logger = get_logger(__name__)


# =============================================================================
# Exit Code Handling
# =============================================================================


class TestExitCode(IntEnum):
    """Exit codes from run_tests script (v2.4.0).

    Based on the competition's run_tests script:
    - 0: Test passed as expected
    - 201 (die): Fatal error - Docker/script issues
    - 202 (fail): Test failed or unexpected result
    - 125, 126, 127, 137: Docker mount/execution failures
    """

    SUCCESS = 0
    FATAL_ERROR = 201  # die() - Docker failed, script not found, etc.
    TEST_FAILURE = 202  # fail() - Test failed or unexpected pass
    DOCKER_DAEMON_ERROR = 125  # Docker daemon error
    DOCKER_CANNOT_INVOKE = 126  # Cannot invoke command
    DOCKER_COMMAND_NOT_FOUND = 127  # Command not found
    DOCKER_KILLED = 137  # Container killed (OOM, timeout)


class TestExecutionError(Exception):
    """Exception raised when test execution fails."""

    def __init__(
        self, message: str, exit_code: int = -1, stdout: str = "", stderr: str = ""
    ):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self):
        """Return detailed error message including stdout/stderr."""
        parts = [self.message]
        if self.stdout:
            parts.append(f"\n\n--- STDOUT ---\n{self.stdout}")
        if self.stderr:
            parts.append(f"\n\n--- STDERR ---\n{self.stderr}")
        return "".join(parts)

    def __reduce__(self):
        """Support pickling for multiprocessing."""
        return (
            self.__class__,
            (self.message, self.exit_code, self.stdout, self.stderr),
        )


class DockerExecutionError(TestExecutionError):
    """Exception raised when Docker fails to mount or run."""


class TestFailedError(TestExecutionError):
    """Exception raised when test fails (exit code 202)."""


class FatalTestError(TestExecutionError):
    """Exception raised for fatal test errors (exit code 201)."""


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class CommandResult:
    """Result of a command execution."""

    stdout: str
    stderr: str
    returncode: int
    success: bool
    timed_out: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "success": self.success,
            "timed_out": self.timed_out,
        }


@dataclass
class TestResult:
    """Result of test.sh execution."""

    stdout: str
    stderr: str
    returncode: int
    success: bool
    timed_out: bool = False
    exit_code_type: Optional[TestExitCode] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MCP compatibility."""
        return {
            "success": self.success,
            "returncode": self.returncode,
            "output": self.stdout + self.stderr,
            "timed_out": self.timed_out,
            "exit_code_type": self.exit_code_type.name if self.exit_code_type else None,
        }


@dataclass
class BuildResult:
    """Result of benchmark build."""

    success: bool
    logs: str
    returncode: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MCP compatibility."""
        return {
            "success": self.success,
            "logs": self.logs,
        }


@dataclass
class PatchResult:
    """Result of patch verification."""

    valid: bool
    test_passed: bool
    patch_applied: bool
    output: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MCP compatibility."""
        return {
            "valid": self.valid,
            "test_passed": self.test_passed,
            "patch_applied": self.patch_applied,
            "output": self.output,
        }


# =============================================================================
# Docker Utilities
# =============================================================================


def docker_image_exists(image_tag: str) -> bool:
    """Check if a Docker image exists locally.

    Args:
        image_tag: Docker image tag (e.g., "crsbench/benchmark-name")

    Returns:
        True if image exists locally, False otherwise
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,  # Prevent terminal issues
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Failed to check docker image {image_tag}: {e}")
        return False


# =============================================================================
# Configuration
# =============================================================================


def get_oss_fuzz_root() -> str:
    """Get OSS-Fuzz root directory.

    Priority:
    1. Managed sparse checkout at third_party/oss-fuzz

    Returns:
        Path to OSS-Fuzz root directory

    Raises:
        RuntimeError: If OSS-Fuzz directory not found
    """
    crsbench_root = Path(__file__).parent.parent.parent
    managed_path = crsbench_root / "third_party" / "oss-fuzz"

    # Managed sparse checkout under third_party/
    helper_py = managed_path / "infra" / "helper.py"
    if helper_py.exists():
        return str(managed_path)

    # None found - raise error
    raise RuntimeError(
        f"OSS-Fuzz not found. Searched:\n"
        f"  1. Managed sparse checkout: {managed_path}\n"
        f"Run scripts/setup-third-party.sh to fetch official oss-fuzz."
    )


def ensure_oss_fuzz_root(*, bootstrap_if_missing: bool = True) -> str:
    """Resolve OSS-Fuzz root, optionally bootstrapping managed checkout.

    Args:
        bootstrap_if_missing: If True, run scripts/setup-third-party.sh when
            OSS-Fuzz cannot be resolved by get_oss_fuzz_root().

    Returns:
        Path to OSS-Fuzz root.

    Raises:
        RuntimeError: If OSS-Fuzz cannot be resolved or bootstrap fails.
    """
    first_error: RuntimeError | None = None
    try:
        return get_oss_fuzz_root()
    except RuntimeError as e:
        first_error = e
        if not bootstrap_if_missing:
            raise

    crsbench_root = Path(__file__).parent.parent.parent
    setup_script = crsbench_root / "scripts" / "setup-third-party.sh"
    if not setup_script.exists():
        assert first_error is not None
        raise first_error

    try:
        lock_dir = crsbench_root / ".crsbench-repos"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / ".oss-fuzz-setup.lock"

        try:
            import fcntl
        except ImportError as e:
            raise RuntimeError(
                "Managed oss-fuzz bootstrap requires file locking support "
                "(fcntl unavailable). Run scripts/setup-third-party.sh once in a "
                "single process, then retry."
            ) from e

        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    # Another process may have completed setup while we waited.
                    return get_oss_fuzz_root()
                except RuntimeError:
                    pass

                result = subprocess.run(
                    ["bash", str(setup_script)],
                    cwd=str(crsbench_root),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        "Failed to bootstrap managed oss-fuzz checkout via "
                        f"{setup_script} (exit={result.returncode}).\n"
                        f"stdout:\n{result.stdout}\n"
                        f"stderr:\n{result.stderr}"
                    )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        return get_oss_fuzz_root()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            "Failed to bootstrap managed oss-fuzz checkout due to unexpected error: "
            f"{type(e).__name__}: {e}"
        ) from e


def get_benchmarks_root() -> str:
    """Get benchmarks root directory from environment or default."""
    default = Path(__file__).parent.parent.parent / "benchmarks"
    return os.getenv("BENCHMARKS_ROOT", str(default.resolve()))


def get_benchmark_dir(benchmark_name: str) -> Path:
    """Get benchmark directory path."""
    return Path(get_benchmarks_root()) / benchmark_name


# =============================================================================
# Utility Functions
# =============================================================================


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", text)


def shorten_logs(log_string: str, max_length: int = 5000) -> str:
    """Shorten log string if it exceeds max length."""
    if len(log_string) > max_length:
        return log_string[:1000] + "\n... [truncated] ...\n" + log_string[-3700:]
    return log_string


def get_workdir_from_dockerfile(benchmark_dir: Union[str, Path]) -> str:
    """Parse WORKDIR from Dockerfile for the benchmark.

    This follows OSS-Fuzz's helper.py logic to extract the last WORKDIR
    directive from the Dockerfile.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        Absolute path to the working directory (e.g., '/src/commons-compress')
        Defaults to '/src' if no WORKDIR found
    """
    benchmark_dir = Path(benchmark_dir)
    dockerfile_path = benchmark_dir / "Dockerfile"

    if not dockerfile_path.exists():
        logger.warning(f"Dockerfile not found at {dockerfile_path}, using default /src")
        return "/src"

    workdir_regex = re.compile(r"\s*WORKDIR\s*([^\s]+)")
    workdir = "/src"  # default

    try:
        with dockerfile_path.open() as f:
            lines = f.readlines()

        # Parse in reverse to get the last WORKDIR directive
        for line in reversed(lines):
            match = workdir_regex.match(line)
            if match:
                workdir = match.group(1)
                # Replace $SRC with /src
                workdir = workdir.replace("$SRC", "/src")

                # Make absolute path if relative
                if not Path(workdir).is_absolute():
                    workdir = f"/src/{workdir}"

                # Normalize the path (don't resolve since this is a Docker path)
                workdir = str(Path(workdir))
                break
    except Exception as e:
        logger.warning(
            f"Failed to parse WORKDIR from Dockerfile: {e}, using default /src"
        )
        return "/src"

    logger.debug(f"Parsed WORKDIR from Dockerfile: {workdir}")
    return workdir


def detect_language(benchmark_dir: Union[str, Path]) -> str:
    """Detect language from project.yaml."""
    project_yaml = Path(benchmark_dir) / "project.yaml"
    if not project_yaml.exists():
        return "unknown"

    try:
        with project_yaml.open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("language:"):
                    return line.split("language:")[1].strip().lower()
    except Exception:
        pass

    return "unknown"


def get_project_config(benchmark_dir: Union[str, Path]) -> Dict[str, Any]:
    """Read and parse project.yaml configuration.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        Dictionary with project configuration
    """
    import yaml

    project_yaml = Path(benchmark_dir) / "project.yaml"
    if not project_yaml.exists():
        return {}

    with project_yaml.open() as f:
        return yaml.safe_load(f) or {}


# =============================================================================
# Command Execution
# =============================================================================


def run_cmd(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    *,
    expect_fail: bool = False,
    exception: bool = True,
    return_code: bool = False,
) -> Union[Tuple[str, str], Tuple[str, str, int]]:
    """Run a command and return stdout/stderr.

    Args:
        cmd: Command and arguments
        cwd: Working directory
        expect_fail: Whether command is expected to fail
        exception: Whether to raise exception on unexpected result
        return_code: Whether to return exit code as third element
        timeout: Optional timeout in seconds

    Returns:
        Tuple of (stdout, stderr) or (stdout, stderr, exit_code) if return_code=True
    """
    logger.debug(f"Running command (cwd: {cwd}): {' '.join(cmd)}")
    sys.stdout.flush()

    # Use run_git for git commands to support gitcache
    if cmd and cmd[0] == "git":
        try:
            result = run_git(
                cmd[1:],  # Skip 'git' since run_git adds it
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = strip_ansi(result.stdout) if result.stdout else ""
            stderr = strip_ansi(result.stderr) if result.stderr else ""
            process_returncode = result.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
            process_returncode = -1
            if return_code:
                return stdout, stderr, -1
            raise RuntimeError(f"Command timed out after {timeout} seconds") from None
    else:
        try:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )

            stdout, stderr = process.communicate(timeout=timeout)
            process_returncode = process.returncode
        except subprocess.TimeoutExpired:
            # Kill entire process group to avoid orphaned Docker containers
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Fallback: kill direct child if killpg missed it
            try:
                process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            stdout, stderr = process.communicate()
            process_returncode = -1
            if return_code:
                stdout = strip_ansi(stdout) if stdout else ""
                stderr = strip_ansi(stderr) if stderr else ""
                return stdout, stderr, -1
            raise RuntimeError(f"Command timed out after {timeout} seconds") from None

        # Clean ANSI escape sequences
        stdout = strip_ansi(stdout) if stdout else ""
        stderr = strip_ansi(stderr) if stderr else ""

    sys.stdout.flush()
    sys.stderr.flush()

    if expect_fail:
        if process_returncode == 0 and exception:
            raise RuntimeError(
                f"Command '{' '.join(cmd)}' succeeded but was expected to fail\n"
                f"returncode: {process_returncode}\n"
                f"stdout: {stdout}\n"
                f"stderr: {stderr}"
            )
    else:
        if process_returncode != 0 and exception:
            raise RuntimeError(
                f"Command '{' '.join(cmd)}' failed with return code {process_returncode}\n"
                f"stdout: {stdout}\n"
                f"stderr: {stderr}"
            )

    if return_code:
        return stdout, stderr, process_returncode
    return stdout, stderr


def run_cmd_with_logging(
    cmd: List[str],
    log_file: Union[str, Path],
    cwd: Optional[str] = None,
    timeout: int = 1200,
) -> CommandResult:
    """Run a command with output logged to file.

    Args:
        cmd: Command and arguments
        log_file: Path to log file
        cwd: Working directory
        timeout: Timeout in seconds (default: 20 minutes)

    Returns:
        CommandResult with execution details
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if log_path.is_file():
        log_path.unlink()

    timed_out = False
    returncode = 0

    with log_path.open("w", encoding="utf-8") as log_stdout:
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=log_stdout,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            process.communicate(timeout=timeout)
            returncode = process.returncode
            if returncode == 0:
                log_stdout.write("\n\nCommand succeeded.\n")
            else:
                log_stdout.write(f"\n\nCommand failed with exit code {returncode}\n")
        except subprocess.TimeoutExpired:
            # Kill entire process group to avoid orphaned Docker containers
            if process is not None:
                try:
                    pgid = os.getpgid(process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                process.wait()
            logger.warning(f"Command timed out after {timeout}s")
            log_stdout.write(f"\n\nCommand timed out after {timeout} seconds.\n")
            returncode = -1
            timed_out = True

    logs = log_path.read_text(encoding="utf-8")

    return CommandResult(
        stdout=logs,
        stderr="",
        returncode=returncode,
        success=returncode == 0,
        timed_out=timed_out,
    )


# =============================================================================
# OSS-Fuzz Helper
# =============================================================================


def run_helper(
    helper_command: List[str],
    oss_fuzz_root: Optional[str] = None,
    *,
    expect_fail: bool = False,
    exception: bool = True,
) -> Tuple[str, str]:
    """Run OSS-Fuzz helper.py command.

    Args:
        helper_command: Arguments to pass to helper.py
        expect_fail: Whether command is expected to fail
        exception: Whether to raise exception on unexpected result
        oss_fuzz_root: OSS-Fuzz root directory (uses env var if not provided)

    Returns:
        Tuple of (stdout, stderr)
    """
    if oss_fuzz_root is None:
        oss_fuzz_root = ensure_oss_fuzz_root()

    helper_path = Path(oss_fuzz_root) / "infra" / "helper.py"
    command = ["python", str(helper_path)] + helper_command
    result = run_cmd(command, expect_fail=expect_fail, exception=exception)
    return cast("Tuple[str, str]", result)


# =============================================================================
# Exit Code Handling
# =============================================================================


def is_docker_execution_error(exit_code: int) -> bool:
    """Check if exit code indicates Docker execution failure."""
    return exit_code in (
        TestExitCode.DOCKER_DAEMON_ERROR,
        TestExitCode.DOCKER_CANNOT_INVOKE,
        TestExitCode.DOCKER_COMMAND_NOT_FOUND,
        TestExitCode.DOCKER_KILLED,
    )


def classify_exit_code(exit_code: int) -> Optional[TestExitCode]:
    """Classify exit code into TestExitCode enum."""
    try:
        return TestExitCode(exit_code)
    except ValueError:
        return None


def handle_test_exit_code(
    exit_code: int,
    expect_success: bool,
    stdout: str,
    stderr: str,
    benchmark: str,
    *,
    raise_exception: bool = True,
) -> TestResult:
    """Handle test.sh exit code according to run_tests script semantics.

    Exit codes from run_tests script (v2.4.0):
    - 0: Test passed as expected
    - 201 (die): Fatal error - Docker/script issues
    - 202 (fail): Test failed or unexpected result
    - 125, 126, 127, 137: Docker mount/execution failures

    Args:
        exit_code: Process exit code
        expect_success: Whether test was expected to succeed
        stdout: Standard output
        stderr: Standard error
        benchmark: Benchmark name for error messages
        raise_exception: Whether to raise exceptions on errors
        docker_command: Full docker command string for debugging

    Returns:
        TestResult with classification

    Raises:
        DockerExecutionError: For Docker mount/execution failures (125, 126, 127, 137)
        FatalTestError: For fatal errors (201)
        TestFailedError: For test failures (202)
        RuntimeError: For unexpected results
    """
    exit_code_type = classify_exit_code(exit_code)
    stdout + stderr

    # Handle Docker execution errors (always fatal)
    if is_docker_execution_error(exit_code):
        result = TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=False,
            exit_code_type=exit_code_type,
        )
        if raise_exception:
            raise DockerExecutionError(
                f"Docker failed to mount and run test.sh for {benchmark}. "
                f"Exit code: {exit_code}",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return result

    # Handle fatal error (die)
    if exit_code == TestExitCode.FATAL_ERROR:
        result = TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=False,
            exit_code_type=TestExitCode.FATAL_ERROR,
        )
        if raise_exception:
            raise FatalTestError(
                f"Fatal error running test.sh for {benchmark}: "
                f"Docker/script issue or test.sh not executable",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return result

    # Handle test failure (fail)
    if exit_code == TestExitCode.TEST_FAILURE:
        if expect_success:
            result = TestResult(
                stdout=stdout,
                stderr=stderr,
                returncode=exit_code,
                success=False,
                exit_code_type=TestExitCode.TEST_FAILURE,
            )
            if raise_exception:
                raise TestFailedError(
                    f"Test failed for {benchmark} but was expected to pass",
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                )
            return result
        logger.info(f"✓ test.sh correctly failed for {benchmark} (exit code 202)")
        return TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=True,  # Expected failure
            exit_code_type=TestExitCode.TEST_FAILURE,
        )

    # Handle success
    if exit_code == TestExitCode.SUCCESS:
        if expect_success:
            logger.info(f"✓ test.sh succeeded for {benchmark}")
            return TestResult(
                stdout=stdout,
                stderr=stderr,
                returncode=exit_code,
                success=True,
                exit_code_type=TestExitCode.SUCCESS,
            )
        result = TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=False,
            exit_code_type=TestExitCode.SUCCESS,
        )
        if raise_exception:
            raise TestFailedError(
                f"Test passed for {benchmark} but was expected to fail",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        return result

    # Handle other non-zero exit codes
    if exit_code != 0:
        if expect_success:
            result = TestResult(
                stdout=stdout,
                stderr=stderr,
                returncode=exit_code,
                success=False,
                exit_code_type=exit_code_type,
            )
            if raise_exception:
                raise RuntimeError(
                    f"test.sh failed for {benchmark} with unexpected exit code {exit_code}\n"
                    f"stdout: {stdout}\n"
                    f"stderr: {stderr}"
                )
            return result
        logger.info(
            f"✓ test.sh failed as expected for {benchmark} (exit code {exit_code})"
        )
        return TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=True,  # Expected failure
            exit_code_type=exit_code_type,
        )

    # Should not reach here
    return TestResult(
        stdout=stdout,
        stderr=stderr,
        returncode=exit_code,
        success=True,
        exit_code_type=exit_code_type,
    )


# =============================================================================
# Source Code Management
# =============================================================================


def get_project_source_dir(
    benchmark_name: str,
    commit: Optional[str] = None,
    *,
    use_ref_commit_for_delta: bool = True,
) -> Optional[str]:
    """Get project source directory, ensuring it's at the correct commit.

    Args:
        benchmark_name: Name of the benchmark
        commit: Specific commit to use (overrides automatic detection)
        use_ref_commit_for_delta: For delta mode, use ref_commit (vulnerable version)

    Returns:
        Path to project source directory or None if failed
    """
    benchmark_dir = get_benchmark_dir(benchmark_name)

    if not benchmark_dir.is_dir():
        logger.error(f"Benchmark directory not found: {benchmark_dir}")
        return None

    # Determine which commit to use
    if commit is None:
        try:
            repo_info = get_repo_info_from_benchmark(str(benchmark_dir))

            # Delta mode: use ref_commit (vulnerable version)
            if use_ref_commit_for_delta and repo_info.ref_commit:
                commit = repo_info.ref_commit
                logger.debug(f"Delta mode: using ref_commit (vulnerable) {commit[:8]}")
            # Full mode: use base_commit (vulnerable version)
            else:
                commit = repo_info.base_commit
                logger.debug(
                    f"Full mode: using base_commit {commit[:8] if commit else 'default'}"
                )
        except Exception as e:
            logger.warning(f"Failed to determine commit, using default: {e}")

    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=os.getenv("PROJECT_REPOS_DIR"),
        commit=commit,
        verbose=True,
    )

    if not source_path:
        logger.error(f"Failed to get project source for benchmark {benchmark_name}")
        return None

    logger.debug(f"Using project source at: {source_path}")
    return source_path


# =============================================================================
# OSS-Fuzz Project Management
# =============================================================================


def prepare_benchmark_for_oss_fuzz(
    benchmark_name: str,
    oss_fuzz_root: Optional[str] = None,
) -> Optional[str]:
    """Prepare benchmark for OSS-Fuzz by copying to oss-fuzz/projects/.

    Args:
        benchmark_name: Name of the benchmark
        oss_fuzz_root: OSS-Fuzz root directory

    Returns:
        The OSS-Fuzz project path (e.g., "aixcc/benchmark-name") or None
    """
    if oss_fuzz_root is None:
        oss_fuzz_root = ensure_oss_fuzz_root()

    benchmark_dir = get_benchmark_dir(benchmark_name)

    if not benchmark_dir.is_dir():
        logger.error(f"Benchmark directory not found: {benchmark_dir}")
        return None

    # Create aixcc directory in oss-fuzz/projects if it doesn't exist
    aixcc_dir = Path(oss_fuzz_root) / "projects" / "aixcc"
    aixcc_dir.mkdir(parents=True, exist_ok=True)

    # Target directory
    target_dir = aixcc_dir / benchmark_name

    # Remove existing directory if it exists
    if target_dir.exists():
        shutil.rmtree(target_dir)

    # Copy benchmark to oss-fuzz/projects/aixcc/
    shutil.copytree(str(benchmark_dir), str(target_dir))
    logger.debug(f"Copied benchmark {benchmark_name} to {target_dir}")

    return f"aixcc/{benchmark_name}"


# =============================================================================
# Build Functions
# =============================================================================


def build_benchmark(
    benchmark: str,
    engine: str = "libfuzzer",
    sanitizer: str = "address",
    commit: Optional[str] = None,
    oss_fuzz_root: Optional[str] = None,
    *,
    clean: bool = True,
    enable_check_build: bool = False,
) -> None:
    """Build a benchmark using OSS-Fuzz infrastructure.

    This function:
    1. Ensures the project source code is cloned (using repo_manager)
    2. Passes the source code path to OSS-Fuzz build_fuzzers
    3. Optionally runs check_build to verify the build

    Args:
        benchmark: Benchmark name (e.g., "curl-delta-02")
        engine: Fuzzing engine (e.g., "libfuzzer")
        sanitizer: Sanitizer (e.g., "address")
        clean: Whether to clean before building
        enable_check_build: Enable check_build validation
        commit: Specific commit to checkout
        oss_fuzz_root: OSS-Fuzz root directory
    """
    logger.info(
        f"Building benchmark {benchmark} with engine={engine} sanitizer={sanitizer}"
    )

    benchmark_dir = get_benchmark_dir(benchmark)

    if not benchmark_dir.exists():
        raise RuntimeError(f"[Error] Benchmark directory not found: {benchmark_dir}")

    # Ensure project source code is cloned
    source_path = get_project_source_dir(benchmark, commit=commit)

    if not source_path:
        raise RuntimeError(
            f"[Error] Failed to obtain source code for {benchmark}. "
            "Check that PROJECT_REPOS_DIR is set and project.yaml has valid main_repo."
        )

    logger.info(f"Using source code at: {source_path}")

    # Build using OSS-Fuzz
    helper_command = ["build_fuzzers"]

    if clean:
        helper_command.append("--clean")

    helper_command.extend(
        [
            "--engine",
            engine,
            "--sanitizer",
            sanitizer,
            "--architecture",
            "x86_64",
            benchmark,
            source_path,
        ]
    )

    run_helper(helper_command, oss_fuzz_root=oss_fuzz_root)

    if enable_check_build:
        check_build(benchmark, engine, sanitizer, oss_fuzz_root=oss_fuzz_root)
    else:
        logger.debug("Skipping check_build (disabled by default for speed)")


def build_benchmark_with_logging(
    benchmark_name: str,
    source_path: Optional[str] = None,
    oss_fuzz_root: Optional[str] = None,
    log_dir: Optional[str] = None,
    timeout: int = 1200,
) -> BuildResult:
    """Build a benchmark with detailed logging (for MCP server).

    Args:
        benchmark_name: Name of the benchmark
        source_path: Path to project source (auto-detected if None)
        oss_fuzz_root: OSS-Fuzz root directory
        log_dir: Directory for log files
        timeout: Build timeout in seconds

    Returns:
        BuildResult with build status and logs
    """
    if oss_fuzz_root is None:
        oss_fuzz_root = ensure_oss_fuzz_root()

    oss_fuzz_path = Path(oss_fuzz_root)
    log_dir_path: Path
    if log_dir is None:
        log_dir_path = oss_fuzz_path / "build" / "logs"
    else:
        log_dir_path = Path(log_dir)

    # Prepare benchmark in oss-fuzz/projects/aixcc/
    oss_fuzz_project = prepare_benchmark_for_oss_fuzz(benchmark_name, oss_fuzz_root)
    if not oss_fuzz_project:
        return BuildResult(
            success=False, logs=f"Error: Failed to prepare benchmark {benchmark_name}"
        )

    # Get project source directory path
    if source_path is None:
        source_path = get_project_source_dir(benchmark_name)

    if not source_path or not Path(source_path).is_dir():
        return BuildResult(
            success=False,
            logs=f"Error: Could not find project source directory for '{benchmark_name}'",
        )

    logger.info(f"Building benchmark '{benchmark_name}' with source: {source_path}")

    # Build command
    helper_path = oss_fuzz_path / "infra" / "helper.py"
    build_cmd = [
        "python3",
        str(helper_path),
        "build_fuzzers",
        oss_fuzz_project,
        source_path,
    ]

    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_file = log_dir_path / f"build-log-{benchmark_name}.txt"

    result = run_cmd_with_logging(
        build_cmd, log_file, cwd=oss_fuzz_root, timeout=timeout
    )

    return BuildResult(
        success=result.success,
        logs=result.stdout,  # Full logs for CI - use shorten_logs() only for display
        returncode=result.returncode,
    )


def check_build(
    benchmark: str,
    engine: str,
    sanitizer: str,
    oss_fuzz_root: Optional[str] = None,
) -> None:
    """Run check_build to verify fuzzers work correctly.

    Args:
        benchmark: Benchmark name
        engine: Fuzzing engine
        sanitizer: Sanitizer
        oss_fuzz_root: OSS-Fuzz root directory
    """
    logger.info(f"Running check_build for {benchmark}")

    helper_command = [
        "check_build",
        "--engine",
        engine,
        "--sanitizer",
        sanitizer,
        "--architecture",
        "x86_64",
        benchmark,
    ]

    run_helper(helper_command, oss_fuzz_root=oss_fuzz_root)


# =============================================================================
# Test Execution
# =============================================================================


def run_test_sh(
    benchmark: str,
    output_dir: Optional[Path] = None,
    commit: Optional[str] = None,
    timeout: int = 600,
    oss_fuzz_root: Optional[str] = None,
    log_dir: Optional[str] = None,
    source_path: Optional[str] = None,
    *,
    expect_success: bool = True,
    raise_exception: bool = True,
    privileged: bool = True,
) -> TestResult:
    """Run test.sh for a benchmark inside Docker container.

    Exit code handling follows the competition's run_tests script (v2.4.0):
    - Exit 0: Test passed
    - Exit 201 (die): Fatal error - Docker/script issues
    - Exit 202 (fail): Test failed or unexpected result
    - Exit 125, 126, 127, 137: Docker mount/execution failures

    Args:
        benchmark: Benchmark name
        expect_success: Whether test.sh is expected to succeed
        output_dir: Directory to save test.sh outputs (stdout, stderr, exit_code files)
        commit: Specific commit to use for source code
        raise_exception: Whether to raise exceptions on errors
        timeout: Timeout in seconds (default: 600)
        privileged: Whether to use --privileged flag for Docker
        oss_fuzz_root: OSS-Fuzz root directory
        log_dir: Directory for log files (if specified, logs to file instead of memory)
        source_path: Path to project source (auto-detected if None)

    Returns:
        TestResult with execution details

    Raises:
        DockerExecutionError: For Docker mount/execution failures
        FatalTestError: For fatal errors (exit code 201)
        TestFailedError: For test failures (exit code 202)
        RuntimeError: For other unexpected results
    """
    if oss_fuzz_root is None:
        oss_fuzz_root = ensure_oss_fuzz_root()

    benchmark_dir = get_benchmark_dir(benchmark)
    test_sh_path = benchmark_dir / "test.sh"

    if not test_sh_path.exists():
        error_msg = f"test.sh not found for benchmark: {benchmark}"
        if log_dir is not None:
            # MCP server mode: return error result
            return TestResult(
                stdout="", stderr=f"Error: {error_msg}", returncode=-1, success=False
            )
        # CI mode: skip with warning
        logger.warning(f"{error_msg}, skipping")
        return TestResult(
            stdout="", stderr="test.sh not found", returncode=0, success=True
        )

    # Get source path
    if source_path is None:
        source_path = get_project_source_dir(benchmark, commit=commit)

    if not source_path or not Path(source_path).is_dir():
        error_msg = f"Project source directory not found for {benchmark}"
        if log_dir is not None:
            return TestResult(
                stdout="", stderr=f"Error: {error_msg}", returncode=-1, success=False
            )
        raise RuntimeError(f"[Error] Failed to get source path for {benchmark}")

    logger.info(
        f"Running test.sh for {benchmark} inside Docker, expect_success={expect_success}"
    )

    # Ensure project directories exist
    oss_fuzz_path = Path(oss_fuzz_root)
    project_out = oss_fuzz_path / "build" / "out" / benchmark
    project_work = oss_fuzz_path / "build" / "work" / benchmark
    project_out.mkdir(parents=True, exist_ok=True)
    project_work.mkdir(parents=True, exist_ok=True)

    # Make test.sh executable
    if not os.access(test_sh_path, os.X_OK):
        test_sh_path.chmod(0o755)

    # Parse WORKDIR from Dockerfile
    workdir = get_workdir_from_dockerfile(benchmark_dir)

    # Read project.yaml for environment variables
    project_config = get_project_config(benchmark_dir)

    # Language mapping
    language = project_config.get("language", "c++")
    language_map = {
        "jvm": "jvm",
        "java": "jvm",
        "c": "c",
        "c++": "c++",
        "go": "go",
        "rust": "rust",
        "python": "python",
    }
    fuzzing_language = language_map.get(language.lower(), language)

    # Engine and sanitizer
    fuzzing_engines = project_config.get("fuzzing_engines", ["libfuzzer"])
    fuzzing_engine = fuzzing_engines[0] if fuzzing_engines else "libfuzzer"

    sanitizers = project_config.get("sanitizers", ["address"])
    sanitizer = sanitizers[0] if sanitizers else "address"

    # Docker image tag
    image_tag = f"crsbench/{benchmark}"

    # Build Docker command
    docker_command = ["docker", "run"]

    if privileged:
        docker_command.extend(
            [
                "--privileged",
                "--shm-size=2g",
                "--platform",
                "linux/amd64",
            ]
        )

    docker_command.append("--rm")

    # Environment variables (always set for consistency)
    docker_command.extend(
        [
            "-e",
            f"FUZZING_ENGINE={fuzzing_engine}",
            "-e",
            f"SANITIZER={sanitizer}",
            "-e",
            "ARCHITECTURE=x86_64",
            "-e",
            "HELPER=True",
            "-e",
            f"PROJECT_NAME={benchmark}",
            "-e",
            f"FUZZING_LANGUAGE={fuzzing_language}",
        ]
    )

    # Volume mounts
    docker_command.extend(
        [
            "-v",
            f"{source_path}:/local-source-mount",
            "-v",
            f"{test_sh_path}:/test-mnt.sh",
            "-v",
            f"{project_out}:/local-out-mount",
            "-v",
            f"{project_work}:/local-work-mount",
        ]
    )

    # Image and command
    # Copy source, out, and work from mounted volumes to avoid conflicts
    docker_command.extend(
        [
            image_tag,
            "/bin/bash",
            "-c",
            f"pushd $SRC && rm -rf {workdir} "
            f"&& cp -r /local-source-mount {workdir} "
            f"&& cp /test-mnt.sh $SRC/test.sh "
            f"&& popd "
            f"&& rm -rf /out/* && cp -r /local-out-mount/. /out/ 2>/dev/null || true "
            f"&& rm -rf /work/* && cp -r /local-work-mount/. /work/ 2>/dev/null || true "
            f"&& bash $SRC/test.sh",
        ]
    )

    # Build full command string for logging
    docker_command_str = " ".join(docker_command)
    logger.debug(f"Executing Docker command:\n{docker_command_str}")

    # Execute command
    if log_dir is not None:
        # Log to file mode
        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)
        log_file = log_dir_path / f"test-sh-log-{benchmark}.txt"

        result = run_cmd_with_logging(docker_command, log_file, timeout=timeout)
        exit_code_type = classify_exit_code(result.returncode)

        return TestResult(
            stdout=result.stdout,
            stderr="",
            returncode=result.returncode,
            success=result.returncode == 0,
            timed_out=result.timed_out,
            exit_code_type=exit_code_type,
        )

    # In-memory mode
    timed_out = False
    try:
        result = run_cmd(
            docker_command,
            expect_fail=False,
            exception=False,
            return_code=True,
            timeout=timeout,
        )
        stdout, stderr, exit_code = cast("Tuple[str, str, int]", result)
    except RuntimeError as e:
        if "timed out" in str(e):
            timed_out = True
            stdout = str(e)
            stderr = ""
            exit_code = -1
        else:
            raise
    except Exception as e:
        # Catch any unexpected exceptions
        logger.error(f"Unexpected error running Docker command: {e}")
        logger.error(f"Docker command was:\n{docker_command_str}")
        stdout = ""
        stderr = str(e)
        exit_code = -1

    # Save outputs if output_dir specified
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        (output_dir / "test.sh.stdout").write_text(stdout)
        (output_dir / "test.sh.stderr").write_text(stderr)
        (output_dir / "test.sh.exit_code").write_text(str(exit_code))
        # Also save the full docker command for debugging
        (output_dir / "docker_command.txt").write_text(docker_command_str)

        logger.debug(f"Saved test.sh output to: {output_dir} (exit code: {exit_code})")

    if timed_out:
        return TestResult(
            stdout=stdout,
            stderr=stderr,
            returncode=exit_code,
            success=False,
            timed_out=True,
        )

    # Handle exit code
    return handle_test_exit_code(
        exit_code,
        expect_success,
        stdout,
        stderr,
        benchmark,
        raise_exception=raise_exception,
    )


# =============================================================================
# POV Reproduction
# =============================================================================


def reproduce_pov(
    benchmark: str,
    harness_name: str,
    pov_path: str,
    error_token: str,
    expect_crash: bool,
    output_dir: Optional[Path] = None,
    oss_fuzz_root: Optional[str] = None,
) -> Tuple[str, str]:
    """Reproduce a POV to verify it triggers the vulnerability.

    Args:
        benchmark: Benchmark name
        harness_name: Harness name
        pov_path: Path to POV blob file
        error_token: Expected error message token
        expect_crash: Whether crash is expected
        output_dir: Directory to save crash logs
        oss_fuzz_root: OSS-Fuzz root directory

    Returns:
        Tuple of (stdout, stderr)

    Raises:
        Exception: If crash expectation doesn't match reality
    """
    logger.info(
        f"Reproducing POV for {benchmark}/{harness_name}, expect_crash={expect_crash}"
    )

    helper_command = ["reproduce", benchmark, harness_name, pov_path]

    stdout, stderr = run_helper(
        helper_command,
        expect_fail=expect_crash,
        exception=False,
        oss_fuzz_root=oss_fuzz_root,
    )
    stdout_clean = strip_ansi(stdout)

    # Save crash log if output_dir is specified
    if output_dir:
        pov_name = Path(pov_path).stem
        pov_output_dir = output_dir / "povs"
        pov_output_dir.mkdir(parents=True, exist_ok=True)

        (pov_output_dir / f"{harness_name}-{pov_name}.stdout").write_text(stdout_clean)
        (pov_output_dir / f"{harness_name}-{pov_name}.stderr").write_text(stderr)

        if expect_crash and error_token in stdout_clean:
            logger.info(f"Saved crash log to: {pov_output_dir}")

    # Verify error token presence matches expectation
    if expect_crash:
        if error_token not in stdout_clean:
            raise Exception(
                f"[Error] Expected crash with error token '{error_token}' but not found in output\n"
                f"stdout: {stdout_clean}"
            )
        logger.info(f"✓ POV correctly triggers crash with error: {error_token}")
    else:
        if error_token in stdout_clean:
            raise Exception(
                f"[Error] Expected no crash after patch, but error token '{error_token}' found in output\n"
                f"stdout: {stdout_clean}"
            )
        logger.info("✓ POV correctly does not crash (patch works)")

    return stdout_clean, stderr


# =============================================================================
# Patch Management
# =============================================================================


def apply_patch(
    benchmark: str,
    patch_path: str,
    source_dir: Optional[str] = None,
) -> None:
    """Apply a patch file to benchmark source.

    Args:
        benchmark: Benchmark name
        patch_path: Path to patch file (.diff)
        source_dir: Source directory (auto-detected if None)
    """
    logger.info(f"Applying patch {patch_path} to {benchmark}")

    if source_dir is None:
        source_dir = get_project_source_dir(benchmark)

    if not source_dir:
        raise RuntimeError(f"[Error] Failed to get source directory for {benchmark}")

    logger.debug(f"Applying patch to source directory: {source_dir}")

    # Try git apply first
    result = run_cmd(
        ["git", "apply", patch_path], cwd=source_dir, exception=False, return_code=True
    )
    stdout, stderr, _ = cast("Tuple[str, str, int]", result)

    # If git apply fails, try patch command
    if "error" in stderr.lower() or "fatal" in stderr.lower():
        logger.debug("git apply failed, trying patch command...")
        run_cmd(["patch", "-p1", "-i", patch_path], cwd=source_dir)

    logger.info(f"✓ Patch applied successfully to {source_dir}")


def revert_patch(
    benchmark: str,
    patch_path: str,
    source_dir: Optional[str] = None,
) -> None:
    """Revert a patch file from benchmark source.

    Args:
        benchmark: Benchmark name
        patch_path: Path to patch file (.diff)
        source_dir: Source directory (auto-detected if None)
    """
    logger.info(f"Reverting patch {patch_path} from {benchmark}")

    if source_dir is None:
        source_dir = get_project_source_dir(benchmark)

    if not source_dir:
        raise RuntimeError(f"[Error] Failed to get source directory for {benchmark}")

    logger.debug(f"Reverting patch from source directory: {source_dir}")

    # Try git apply -R first
    result = run_cmd(
        ["git", "apply", "-R", patch_path],
        cwd=source_dir,
        exception=False,
        return_code=True,
    )
    stdout, stderr, _ = cast("Tuple[str, str, int]", result)

    # If git apply fails, try patch -R
    if "error" in stderr.lower() or "fatal" in stderr.lower():
        logger.debug("git apply -R failed, trying patch -R...")
        run_cmd(["patch", "-R", "-p1", "-i", patch_path], cwd=source_dir)

    logger.info(f"✓ Patch reverted successfully from {source_dir}")


def verify_bad_patch(
    benchmark_name: str,
    bad_patch_path: str,
    oss_fuzz_root: Optional[str] = None,
) -> PatchResult:
    """Verify that bad_patch.diff breaks test.sh.

    Workflow:
    1. Apply bad_patch.diff to project source
    2. Run test.sh
    3. Check if test.sh fails (exit code != 0)
    4. Restore project source to original state

    Args:
        benchmark_name: Name of the benchmark
        bad_patch_path: Path to bad_patch.diff file
        oss_fuzz_root: OSS-Fuzz root directory

    Returns:
        PatchResult with verification status
    """
    benchmark_dir = get_benchmark_dir(benchmark_name)

    if not benchmark_dir.is_dir():
        return PatchResult(
            valid=False,
            test_passed=False,
            patch_applied=False,
            output=f"Error: Benchmark directory not found: {benchmark_dir}",
        )

    if not Path(bad_patch_path).exists():
        return PatchResult(
            valid=False,
            test_passed=False,
            patch_applied=False,
            output=f"Error: bad_patch.diff not found at {bad_patch_path}",
        )

    # Get project source directory
    project_src_dir = get_project_source_dir(benchmark_name)
    if not project_src_dir or not Path(project_src_dir).is_dir():
        return PatchResult(
            valid=False,
            test_passed=False,
            patch_applied=False,
            output=f"Error: Project source directory not found for {benchmark_name}",
        )

    logger.info(f"Verifying bad_patch.diff for benchmark '{benchmark_name}'...")

    output_lines = []

    # Step 1: Apply patch
    output_lines.append("=== Step 1: Applying bad_patch.diff ===")
    try:
        apply_patch(benchmark_name, bad_patch_path, source_dir=project_src_dir)
        output_lines.append("✅ Patch applied successfully")
        patch_applied = True
    except Exception as e:
        output_lines.append(f"Error applying patch: {str(e)}")
        return PatchResult(
            valid=False,
            test_passed=False,
            patch_applied=False,
            output="\n".join(output_lines),
        )

    # Step 2: Run test.sh
    output_lines.append("\n=== Step 2: Running test.sh with bad patch ===")
    try:
        test_result = run_test_sh(
            benchmark_name,
            expect_success=True,
            raise_exception=False,
            oss_fuzz_root=oss_fuzz_root,
        )

        test_passed = test_result.success
        output_lines.append(f"test.sh exit code: {test_result.returncode}")
        output_lines.append(f"test.sh passed: {test_passed}")
        output_lines.append(
            f"\ntest.sh output (truncated):\n{shorten_logs(test_result.stdout, 1000)}"
        )

    except Exception as e:
        output_lines.append(f"Error running test.sh: {str(e)}")
        test_passed = False

    # Step 3: Restore original state
    output_lines.append("\n=== Step 3: Restoring original state ===")
    try:
        revert_patch(benchmark_name, bad_patch_path, source_dir=project_src_dir)
        output_lines.append("✅ Patch reversed successfully")
    except Exception as e:
        output_lines.append(f"⚠️  Error reversing patch: {str(e)}")
        output_lines.append("Manual cleanup may be required!")

    # Step 4: Evaluate result
    output_lines.append("\n=== Verification Result ===")

    # Valid bad_patch should:
    # 1. Apply successfully (patch_applied = True)
    # 2. Cause test.sh to fail (test_passed = False)
    is_valid = patch_applied and not test_passed

    if is_valid:
        output_lines.append(
            "✅ VALID: bad_patch.diff causes test.sh to fail (as expected)"
        )
    elif test_passed:
        output_lines.append("❌ INVALID: test.sh PASSED with bad patch applied")
        output_lines.append("   This means either:")
        output_lines.append("   1. bad_patch.diff is not strong enough")
        output_lines.append("   2. test.sh doesn't cover the mutated code")
    else:
        output_lines.append("❌ INVALID: Patch failed to apply or other error occurred")

    return PatchResult(
        valid=is_valid,
        test_passed=test_passed,
        patch_applied=patch_applied,
        output="\n".join(output_lines),
    )


# =============================================================================
# Container Command Execution
# =============================================================================


def run_command_in_container(
    benchmark_name: str,
    command: str,
    timeout: int = 60,
    oss_fuzz_root: Optional[str] = None,
) -> str:
    """Run a command inside the Docker container for a benchmark.

    Args:
        benchmark_name: Name of the benchmark
        command: Command to run inside the container
        timeout: Timeout in seconds
        oss_fuzz_root: OSS-Fuzz root directory

    Returns:
        Command output
    """
    if oss_fuzz_root is None:
        oss_fuzz_root = ensure_oss_fuzz_root()

    # Get project source directory
    project_src_dir = get_project_source_dir(benchmark_name)
    if not project_src_dir or not Path(project_src_dir).is_dir():
        return f"Error: Project source directory not found for {benchmark_name}"

    # Get WORKDIR from Dockerfile
    benchmark_dir = get_benchmark_dir(benchmark_name)
    workdir = get_workdir_from_dockerfile(benchmark_dir)

    # Docker image tag
    image_tag = f"crsbench/{benchmark_name}"

    logger.debug(
        f"Running command in container for benchmark '{benchmark_name}': {command}"
    )

    docker_cmd_parts = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{project_src_dir}:{workdir}:rw",
        image_tag,
        "bash",
        "-c",
        command,
    ]

    try:
        from crsbench.utils.subprocess_utils import run_with_timeout

        result = run_with_timeout(
            docker_cmd_parts,
            timeout=timeout,
        )

        output = result.stdout + result.stderr

        if result.returncode != 0:
            logger.debug(f"Command failed with exit code {result.returncode}")
            return f"Command failed with exit code {result.returncode}\n\nOutput:\n{output}"

        logger.debug(f"Command succeeded for benchmark '{benchmark_name}'")
        return output

    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out for benchmark '{benchmark_name}'")
        return f"Error: Command execution timed out after {timeout} seconds"
    except Exception as e:
        logger.error(f"Failed to run command for benchmark '{benchmark_name}': {e}")
        return f"Error: Failed to run command: {str(e)}"


# =============================================================================
# Benchmark Info
# =============================================================================


def get_benchmark_info(benchmark_name: str) -> Dict[str, Any]:
    """Get information about a benchmark.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Dictionary containing benchmark metadata
    """
    benchmark_dir = get_benchmark_dir(benchmark_name)

    if not benchmark_dir.is_dir():
        return {"error": f"Benchmark directory not found: {benchmark_dir}"}

    info = {
        "benchmark_name": benchmark_name,
        "benchmark_dir": str(benchmark_dir),
        "language": detect_language(benchmark_dir),
        "has_dockerfile": (benchmark_dir / "Dockerfile").exists(),
        "has_build_sh": (benchmark_dir / "build.sh").exists(),
        "has_replay_build_sh": (benchmark_dir / "replay-build.sh").exists(),
        "has_test_sh": (benchmark_dir / "test.sh").exists(),
        "has_project_yaml": (benchmark_dir / "project.yaml").exists(),
    }

    # Get project source directory if available
    project_src = get_project_source_dir(benchmark_name)
    if project_src:
        info["project_source_dir"] = project_src
        info["has_project_source"] = Path(project_src).is_dir()

    return info


# =============================================================================
# Command Execution with Rolling Output Display
# =============================================================================


def run_with_rolling_output(command: str, n: int = 5) -> None:
    """
    Executes a command and dynamically updates the terminal to show
    only the last N lines of output in real-time.

    Args:
        command (str): The command string to execute.
        n (int): The number of recent lines to keep and display. Defaults to 5.

    Raises:
        subprocess.CalledProcessError: If the command exits with a non-zero status.
    """
    # Use a deque (double-ended queue) with a maximum length of N.
    recent_lines_buffer: Deque[str] = deque(maxlen=n)
    lines_printed_count = (
        0  # Tracks how many lines we previously printed to manage cursor
    )
    first_output = True  # Flag to track if this is the first output

    # Get terminal width for calculating wrapped lines
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns

    def count_display_lines(text: str) -> int:
        """Calculate how many terminal lines a string will occupy, accounting for wrapping."""
        if not text:
            return 0
        # Each line in the text may wrap multiple times
        lines = text.split(os.linesep)
        total_display_lines = 0
        for line in lines:
            if len(line) == 0:
                total_display_lines += 1
            else:
                # Calculate how many terminal lines this logical line will occupy
                # Add terminal_width - 1 to ensure we round up
                total_display_lines += (
                    len(line) + terminal_width - 1
                ) // terminal_width
        return total_display_lines

    # We use Popen to start the process non-blockingly and pipe its output
    try:
        # Use Python's built-in stderr=STDOUT for robust output merging
        # start_new_session=True ensures child processes inherit I/O redirections
        # stdin=subprocess.DEVNULL prevents TTY access from nested processes
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # Line-buffered reading
            start_new_session=True,  # Ensures all child processes inherit I/O
        )

        logger.info(f"--- Executing: '{command}' ---")
        logger.info(
            "=============================== COMMAND OUTPUT ==============================="
        )

        # Read the output line by line in real-time
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                clean_line = line.strip()
                if clean_line:
                    # 1. Update the rolling buffer
                    recent_lines_buffer.append(clean_line)

                    # 2. Clear previous output (move cursor up and clear lines)
                    # We move the cursor up by the number of lines we last printed.
                    # Only do this if we've already printed output from this function call
                    if lines_printed_count > 0 and not first_output:
                        sys.stdout.write("\033[1A\033[K" * lines_printed_count)

                    # 3. Print the new state of the buffer
                    current_output = os.linesep.join(list(recent_lines_buffer))
                    # Print the current content of the deque, followed by a newline
                    sys.stdout.write(current_output + os.linesep)

                    # Ensure the output is immediately shown on the console
                    sys.stdout.flush()

                    # 4. Update the tracker with actual display line count
                    lines_printed_count = count_display_lines(current_output)
                    first_output = False  # Mark that we've printed at least once

        # Wait for the process to complete and get the return code
        process.wait()

        # Don't clear the rolling display - leave final output visible
        # Just add a newline to separate from subsequent output
        if lines_printed_count > 0:
            sys.stdout.write(os.linesep)
            sys.stdout.flush()

        logger.info(
            "=============================================================================="
        )

        if process.returncode != 0:
            # Log final error state
            logger.error(f"--- Command FAILED (Exit Code: {process.returncode}) ---")
            logger.error(f"Error executing command: '{command}'")
            logger.error(f"Last {n} lines of output/error before exit:")
            if recent_lines_buffer:
                logger.error(os.linesep.join(list(recent_lines_buffer)))
            else:
                logger.error("[No output captured]")
            # Re-raise the exception for caller to handle
            raise subprocess.CalledProcessError(process.returncode, command)

    except subprocess.CalledProcessError:
        # Re-raise CalledProcessError for caller to handle
        raise

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        raise
