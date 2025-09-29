"""Harness execution engine for POV reproduction."""

import os
import subprocess
import tempfile
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, List, Union

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result from harness execution."""
    return_code: int
    stdout: str
    stderr: str
    execution_time: float
    timed_out: bool = False


class HarnessExecutor:
    """Executes fuzzing harnesses with POV inputs."""

    def __init__(self,
                 timeout: int = 30,
                 build_timeout: int = 60,
                 temp_dir: Optional[Path] = None):
        """Initialize harness executor.

        Args:
            timeout: Maximum execution time in seconds
            build_timeout: Maximum build time in seconds
            temp_dir: Directory for temporary files
        """
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.temp_dir = temp_dir or Path(tempfile.gettempdir())

    def execute_harness(self,
                       harness_path: Path,
                       pov_input: Optional[bytes] = None,
                       sanitizer: str = "address") -> ExecutionResult:
        """Execute a harness with POV input.

        Args:
            harness_path: Path to harness source file
            pov_input: Input data to feed to the harness
            sanitizer: Sanitizer to use (address, memory, undefined, etc.)

        Returns:
            ExecutionResult with execution details
        """
        logger.info(f"Executing harness: {harness_path}")

        # Build the harness
        executable_path = self._build_harness(harness_path, sanitizer)
        if not executable_path:
            return ExecutionResult(
                return_code=-1,
                stdout="",
                stderr="Failed to build harness",
                execution_time=0.0,
                timed_out=False
            )

        # Execute the harness
        return self._run_executable(executable_path, pov_input)

    def _build_harness(self, harness_path: Path, sanitizer: str) -> Optional[Path]:
        """Build the harness with appropriate sanitizer flags.

        Args:
            harness_path: Path to harness source file
            sanitizer: Sanitizer type to enable

        Returns:
            Path to built executable, or None if build failed
        """
        logger.debug(f"Building harness: {harness_path} with {sanitizer} sanitizer")

        # Determine output executable path
        output_path = self.temp_dir / f"harness_{int(time.time())}"

        # Get compiler and flags
        compiler = self._get_compiler(harness_path)
        flags = self._get_compiler_flags(sanitizer)

        # Build command
        build_cmd = [
            compiler,
            str(harness_path),
            "-o", str(output_path)
        ] + flags

        logger.debug(f"Build command: {' '.join(build_cmd)}")

        try:
            # Run build command
            result = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
                timeout=self.build_timeout
            )

            if result.returncode != 0:
                logger.error(f"Build failed: {result.stderr}")
                return None

            if not output_path.exists():
                logger.error("Build completed but executable not found")
                return None

            # Make executable
            output_path.chmod(0o755)
            return output_path

        except subprocess.TimeoutExpired:
            logger.error(f"Build timed out after {self.build_timeout} seconds")
            return None
        except Exception as e:
            logger.error(f"Build error: {e}")
            return None

    def _run_executable(self,
                       executable_path: Path,
                       pov_input: Optional[bytes] = None) -> ExecutionResult:
        """Run the executable with POV input.

        Args:
            executable_path: Path to built executable
            pov_input: Input data to provide to executable

        Returns:
            ExecutionResult with execution details
        """
        logger.debug(f"Running executable: {executable_path}")

        start_time = time.time()
        timed_out = False

        try:
            # Prepare input
            stdin_data = pov_input if pov_input is not None else b""

            # Run executable
            result = subprocess.run(
                [str(executable_path)],
                input=stdin_data,
                capture_output=True,
                timeout=self.timeout
            )

            execution_time = time.time() - start_time

            # Decode output
            stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""

            logger.debug(f"Execution completed in {execution_time:.2f}s with return code {result.returncode}")

            return ExecutionResult(
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                execution_time=execution_time,
                timed_out=False
            )

        except subprocess.TimeoutExpired as e:
            execution_time = time.time() - start_time
            timed_out = True

            # Decode partial output
            stdout = e.stdout.decode('utf-8', errors='replace') if e.stdout else ""
            stderr = e.stderr.decode('utf-8', errors='replace') if e.stderr else ""

            logger.warning(f"Execution timed out after {self.timeout} seconds")

            return ExecutionResult(
                return_code=-1,
                stdout=stdout,
                stderr=stderr + f"\nExecution timed out after {self.timeout} seconds",
                execution_time=execution_time,
                timed_out=True
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Execution error: {e}")

            return ExecutionResult(
                return_code=-1,
                stdout="",
                stderr=f"Execution error: {e}",
                execution_time=execution_time,
                timed_out=False
            )

        finally:
            # Clean up executable
            try:
                if executable_path.exists():
                    executable_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to clean up executable: {e}")

    def _get_compiler(self, source_path: Path) -> str:
        """Get appropriate compiler for source file.

        Args:
            source_path: Path to source file

        Returns:
            Compiler command to use
        """
        suffix = source_path.suffix.lower()

        if suffix in ['.c']:
            # Check for clang first, fall back to gcc
            for compiler in ['clang', 'gcc']:
                if self._command_exists(compiler):
                    return compiler
            return 'cc'  # System default

        elif suffix in ['.cpp', '.cxx', '.cc']:
            # Check for clang++ first, fall back to g++
            for compiler in ['clang++', 'g++']:
                if self._command_exists(compiler):
                    return compiler
            return 'c++'  # System default

        else:
            logger.warning(f"Unknown file extension: {suffix}, using cc")
            return 'cc'

    def _get_compiler_flags(self, sanitizer: str) -> List[str]:
        """Get compiler flags for specified sanitizer.

        Args:
            sanitizer: Sanitizer type

        Returns:
            List of compiler flags
        """
        base_flags = [
            "-g",           # Debug information
            "-O1",          # Light optimization
            "-fno-omit-frame-pointer",  # Keep frame pointers for better stack traces
        ]

        sanitizer_flags = {
            "address": ["-fsanitize=address"],
            "memory": ["-fsanitize=memory"],
            "undefined": ["-fsanitize=undefined"],
            "thread": ["-fsanitize=thread"],
            "leak": ["-fsanitize=leak"],
            "none": []
        }

        flags = base_flags.copy()
        if sanitizer in sanitizer_flags:
            flags.extend(sanitizer_flags[sanitizer])
        else:
            logger.warning(f"Unknown sanitizer: {sanitizer}, using address sanitizer")
            flags.extend(sanitizer_flags["address"])

        return flags

    def _command_exists(self, command: str) -> bool:
        """Check if a command exists in PATH.

        Args:
            command: Command to check

        Returns:
            True if command exists, False otherwise
        """
        try:
            subprocess.run(
                ["which", command],
                capture_output=True,
                check=True
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


class LibFuzzerExecutor(HarnessExecutor):
    """Specialized executor for libFuzzer harnesses."""

    def __init__(self, *args, **kwargs):
        """Initialize libFuzzer executor."""
        super().__init__(*args, **kwargs)

    def _get_compiler_flags(self, sanitizer: str) -> List[str]:
        """Get compiler flags for libFuzzer harnesses."""
        flags = super()._get_compiler_flags(sanitizer)

        # Add libFuzzer flags
        flags.extend([
            "-fsanitize=fuzzer-no-link",  # LibFuzzer instrumentation without main
            "-DFUZZING_BUILD_MODE_UNSAFE_FOR_PRODUCTION"  # Common fuzzing define
        ])

        return flags


class AFLExecutor(HarnessExecutor):
    """Specialized executor for AFL harnesses."""

    def __init__(self, *args, **kwargs):
        """Initialize AFL executor."""
        super().__init__(*args, **kwargs)

    def _get_compiler(self, source_path: Path) -> str:
        """Get AFL compiler if available."""
        suffix = source_path.suffix.lower()

        if suffix in ['.c']:
            if self._command_exists('afl-gcc'):
                return 'afl-gcc'
            elif self._command_exists('afl-clang'):
                return 'afl-clang'
        elif suffix in ['.cpp', '.cxx', '.cc']:
            if self._command_exists('afl-g++'):
                return 'afl-g++'
            elif self._command_exists('afl-clang++'):
                return 'afl-clang++'

        # Fall back to regular compiler
        return super()._get_compiler(source_path)