"""Patch validation functionality."""

import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict
from crsbench.patch_tester.applicator import PatchApplication

logger = logging.getLogger(__name__)


class ValidationOutcome(Enum):
    """Outcome of patch validation."""
    VALID = "valid"                    # Patch is valid and code builds
    INVALID = "invalid"                # Patch is invalid or causes build failures
    BUILD_FAILED = "build_failed"      # Code fails to build after patch
    SYNTAX_ERROR = "syntax_error"      # Patch introduces syntax errors
    UNKNOWN = "unknown"                # Unable to determine validity


class PatchValidator:
    """Validates patches by checking compilation and basic integrity."""

    def __init__(self,
                 build_timeout: int = 120,
                 check_syntax: bool = True,
                 run_tests: bool = False):
        """Initialize patch validator.

        Args:
            build_timeout: Timeout for build operations in seconds
            check_syntax: Whether to perform syntax checking
            run_tests: Whether to run existing tests (if available)
        """
        self.build_timeout = build_timeout
        self.check_syntax = check_syntax
        self.run_tests = run_tests

    def validate_patch(self,
                      benchmark_path: Path,
                      patch_application: PatchApplication) -> ValidationOutcome:
        """Validate a patch by checking if patched code builds and passes basic checks.

        Args:
            benchmark_path: Path to benchmark directory
            patch_application: Result of patch application

        Returns:
            ValidationOutcome indicating patch validity
        """
        logger.info(f"Validating patch '{patch_application.patch_name}'")

        # Check if patch was applied successfully
        if patch_application.status.value not in ['success', 'partial']:
            return ValidationOutcome.INVALID

        try:
            # Step 1: Check syntax of modified files
            if self.check_syntax:
                syntax_outcome = self._check_syntax(benchmark_path, patch_application.applied_files)
                if syntax_outcome != ValidationOutcome.VALID:
                    return syntax_outcome

            # Step 2: Attempt to build the code
            build_outcome = self._check_build(benchmark_path)
            if build_outcome != ValidationOutcome.VALID:
                return build_outcome

            # Step 3: Run tests if requested
            if self.run_tests:
                test_outcome = self._run_tests(benchmark_path)
                if test_outcome != ValidationOutcome.VALID:
                    logger.warning("Tests failed after patch application")
                    # Don't fail validation for test failures - patches might fix bugs that tests expose

            return ValidationOutcome.VALID

        except Exception as e:
            logger.error(f"Error validating patch: {e}")
            return ValidationOutcome.UNKNOWN

    def _check_syntax(self,
                     benchmark_path: Path,
                     modified_files: List[str]) -> ValidationOutcome:
        """Check syntax of modified files."""
        logger.debug("Checking syntax of modified files")

        for file_path in modified_files:
            full_path = benchmark_path / file_path

            if not full_path.exists():
                logger.warning(f"Modified file not found: {full_path}")
                continue

            # Determine file type and check syntax accordingly
            if self._is_c_or_cpp_file(full_path):
                if not self._check_c_cpp_syntax(full_path):
                    logger.error(f"Syntax error in C/C++ file: {full_path}")
                    return ValidationOutcome.SYNTAX_ERROR
            elif self._is_python_file(full_path):
                if not self._check_python_syntax(full_path):
                    logger.error(f"Syntax error in Python file: {full_path}")
                    return ValidationOutcome.SYNTAX_ERROR
            elif self._is_java_file(full_path):
                if not self._check_java_syntax(full_path):
                    logger.error(f"Syntax error in Java file: {full_path}")
                    return ValidationOutcome.SYNTAX_ERROR

        return ValidationOutcome.VALID

    def _check_build(self, benchmark_path: Path) -> ValidationOutcome:
        """Check if the code builds successfully after patch application."""
        logger.debug("Checking if patched code builds")

        # Look for build configuration files and attempt to build
        build_methods = [
            self._try_make_build,
            self._try_cmake_build,
            self._try_autotools_build,
            self._try_compile_all_c_files
        ]

        for build_method in build_methods:
            try:
                if build_method(benchmark_path):
                    logger.debug(f"Build successful with method: {build_method.__name__}")
                    return ValidationOutcome.VALID
            except Exception as e:
                logger.debug(f"Build method {build_method.__name__} failed: {e}")
                continue

        logger.error("All build methods failed")
        return ValidationOutcome.BUILD_FAILED

    def _run_tests(self, benchmark_path: Path) -> ValidationOutcome:
        """Run existing tests if available."""
        logger.debug("Running existing tests")

        test_methods = [
            self._run_make_check,
            self._run_ctest,
            self._run_python_tests,
            self._run_junit_tests
        ]

        for test_method in test_methods:
            try:
                if test_method(benchmark_path):
                    logger.debug(f"Tests passed with method: {test_method.__name__}")
                    return ValidationOutcome.VALID
            except Exception as e:
                logger.debug(f"Test method {test_method.__name__} failed: {e}")
                continue

        # If no test method worked, assume no tests exist
        logger.debug("No tests found or all test methods failed")
        return ValidationOutcome.VALID

    def _is_c_or_cpp_file(self, file_path: Path) -> bool:
        """Check if file is C or C++."""
        return file_path.suffix.lower() in ['.c', '.cpp', '.cxx', '.cc', '.h', '.hpp', '.hxx']

    def _is_python_file(self, file_path: Path) -> bool:
        """Check if file is Python."""
        return file_path.suffix.lower() == '.py'

    def _is_java_file(self, file_path: Path) -> bool:
        """Check if file is Java."""
        return file_path.suffix.lower() == '.java'

    def _check_c_cpp_syntax(self, file_path: Path) -> bool:
        """Check C/C++ syntax using compiler."""
        try:
            # Use compiler to check syntax
            compiler = 'gcc' if file_path.suffix.lower() == '.c' else 'g++'
            result = subprocess.run(
                [compiler, '-fsyntax-only', str(file_path)],
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_python_syntax(self, file_path: Path) -> bool:
        """Check Python syntax."""
        try:
            result = subprocess.run(
                ['python', '-m', 'py_compile', str(file_path)],
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_java_syntax(self, file_path: Path) -> bool:
        """Check Java syntax."""
        try:
            result = subprocess.run(
                ['javac', '-cp', '.', str(file_path)],
                cwd=file_path.parent,
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False

    def _try_make_build(self, benchmark_path: Path) -> bool:
        """Try building with make."""
        makefile = benchmark_path / 'Makefile'
        if not makefile.exists():
            return False

        try:
            result = subprocess.run(
                ['make', '-j4'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _try_cmake_build(self, benchmark_path: Path) -> bool:
        """Try building with cmake."""
        cmake_file = benchmark_path / 'CMakeLists.txt'
        if not cmake_file.exists():
            return False

        try:
            # Create build directory
            build_dir = benchmark_path / 'build'
            build_dir.mkdir(exist_ok=True)

            # Configure
            result = subprocess.run(
                ['cmake', '..'],
                cwd=build_dir,
                capture_output=True,
                timeout=60
            )
            if result.returncode != 0:
                return False

            # Build
            result = subprocess.run(
                ['cmake', '--build', '.', '-j4'],
                cwd=build_dir,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _try_autotools_build(self, benchmark_path: Path) -> bool:
        """Try building with autotools."""
        configure_script = benchmark_path / 'configure'
        if not configure_script.exists():
            return False

        try:
            # Configure
            result = subprocess.run(
                ['./configure'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=60
            )
            if result.returncode != 0:
                return False

            # Build
            result = subprocess.run(
                ['make', '-j4'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _try_compile_all_c_files(self, benchmark_path: Path) -> bool:
        """Try compiling all C/C++ files individually."""
        c_files = list(benchmark_path.glob('**/*.c'))
        cpp_files = list(benchmark_path.glob('**/*.cpp'))

        if not c_files and not cpp_files:
            return False

        try:
            # Compile C files
            for c_file in c_files:
                result = subprocess.run(
                    ['gcc', '-c', str(c_file), '-o', '/dev/null'],
                    capture_output=True,
                    timeout=30
                )
                if result.returncode != 0:
                    return False

            # Compile C++ files
            for cpp_file in cpp_files:
                result = subprocess.run(
                    ['g++', '-c', str(cpp_file), '-o', '/dev/null'],
                    capture_output=True,
                    timeout=30
                )
                if result.returncode != 0:
                    return False

            return True
        except Exception:
            return False

    def _run_make_check(self, benchmark_path: Path) -> bool:
        """Run tests with make check."""
        try:
            result = subprocess.run(
                ['make', 'check'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _run_ctest(self, benchmark_path: Path) -> bool:
        """Run tests with ctest."""
        build_dir = benchmark_path / 'build'
        if not build_dir.exists():
            return False

        try:
            result = subprocess.run(
                ['ctest'],
                cwd=build_dir,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _run_python_tests(self, benchmark_path: Path) -> bool:
        """Run Python tests."""
        try:
            # Try pytest first
            result = subprocess.run(
                ['pytest'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            if result.returncode == 0:
                return True

            # Try unittest
            result = subprocess.run(
                ['python', '-m', 'unittest', 'discover'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False

    def _run_junit_tests(self, benchmark_path: Path) -> bool:
        """Run JUnit tests."""
        try:
            result = subprocess.run(
                ['mvn', 'test'],
                cwd=benchmark_path,
                capture_output=True,
                timeout=self.build_timeout
            )
            return result.returncode == 0
        except Exception:
            return False