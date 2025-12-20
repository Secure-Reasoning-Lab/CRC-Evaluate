"""OSS-Fuzz reproducer for POV validation.

This module provides a wrapper around OSS-Fuzz's helper.py for:
1. Building fuzzers for benchmark variants
2. Reproducing crashes with POV testcases
3. Checking build cache status

Exit code handling (with --propagate_exit_codes):
- 0: No crash (fuzzer ran successfully, no bug found)
- 77: ASAN crash (AddressSanitizer detected memory error)
- 124: Timeout (subprocess or helper.py timeout)
- Other non-zero: Other crash types (UBSAN, MSAN, etc.)
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Exit code constants from helper.py
EXIT_CODE_TIMEOUT = 124  # Subprocess timeout in helper.py


class OSSFuzzReproducer:
    """Wrapper for OSS-Fuzz helper.py operations.

    Provides methods for building fuzzers and reproducing crashes
    using OSS-Fuzz's infrastructure.

    Attributes:
        oss_fuzz_path: Path to the oss-fuzz directory
        timeout: Default timeout for operations in seconds
    """

    def __init__(self, oss_fuzz_path: Path, timeout: int = 120):
        """Initialize the reproducer.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            timeout: Default timeout for subprocess operations
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()  # Use absolute path
        self.timeout = timeout
        self._helper_script = self.oss_fuzz_path / "infra" / "helper.py"

        if not self._helper_script.exists():
            raise FileNotFoundError(
                f"OSS-Fuzz helper.py not found: {self._helper_script}"
            )

    def get_build_output_path(self, project_name: str) -> Path:
        """Get the build output path for a project.

        Args:
            project_name: Project name (e.g., "aixcc/c/afc-curl-delta-01-cpv0")

        Returns:
            Path to the build output directory
        """
        return self.oss_fuzz_path / "build" / "out" / project_name.replace("/", os.sep)

    def is_variant_built(self, project_name: str) -> bool:
        """Check if a variant has been built.

        Args:
            project_name: Project name

        Returns:
            True if built fuzzers exist, False otherwise
        """
        build_path = self.get_build_output_path(project_name)
        return build_path.exists() and any(build_path.iterdir())

    def build_fuzzers(
        self,
        project_name: str,
        src_path: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> bool:
        """Build fuzzers for a project.

        Args:
            project_name: Project name
            src_path: Optional source path for in-place build
            timeout: Optional timeout override

        Returns:
            True if build succeeded, False otherwise
        """
        uid = os.getuid()
        cmd = [
            "python3",
            str(self._helper_script),
            "build_fuzzers",
            "-e", f"BUILD_UID={uid}",
            project_name,
        ]

        if src_path:
            cmd.append(str(src_path))

        logger.info(f"Building fuzzers for {project_name}")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout * 10,  # Build takes longer
            )

            if result.returncode == 0:
                logger.info(f"Successfully built fuzzers for {project_name}")
                return True
            else:
                logger.error(
                    f"Failed to build fuzzers for {project_name}: {result.stderr}"
                )
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"Build timeout for {project_name}")
            return False
        except Exception as e:
            logger.error(f"Build error for {project_name}: {e}")
            return False

    def reproduce(
        self,
        project_name: str,
        harness: str,
        pov_data: bytes,
        timeout: Optional[int] = None,
        request_id: Optional[int] = None,
    ) -> bool:
        """Reproduce a crash using OSS-Fuzz helper.py.

        Uses --propagate_exit_codes to get explicit exit codes:
        - 0: No crash
        - 77: ASAN crash
        - 124: Timeout (treated as no crash)
        - Other non-zero: Other crash types

        Args:
            project_name: Project name
            harness: Harness name to run
            pov_data: Raw POV/testcase bytes
            timeout: Optional timeout override
            request_id: Optional request ID for logging

        Returns:
            True if the POV causes a crash, False otherwise
        """
        # Write POV data to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(pov_data)
            testcase_path = Path(f.name)

        effective_timeout = timeout or self.timeout
        req_prefix = f"[Request #{request_id}] " if request_id else ""

        try:
            cmd = [
                "python3",
                str(self._helper_script),
                "reproduce",
                "--propagate_exit_codes",
                "--timeout", str(effective_timeout),
                project_name,
                harness,
                str(testcase_path),
                "--",
                "-detect_leaks=0",
            ]

            logger.debug(f"{req_prefix}Reproducing: {' '.join(cmd)}")

            # Use slightly longer subprocess timeout to let helper.py handle it
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=effective_timeout + 30,  # Grace period for helper.py
            )

            # Handle exit codes explicitly
            if result.returncode == 0:
                logger.info(f"{req_prefix}{project_name}/{harness} did not crash")
                return False
            elif result.returncode == EXIT_CODE_TIMEOUT:
                logger.info(
                    f"{req_prefix}{project_name}/{harness} timed out (exit code 124)"
                )
                return False
            else:
                logger.info(
                    f"{req_prefix}{project_name}/{harness} crashed "
                    f"(exit code {result.returncode})"
                )
                return True

        except subprocess.TimeoutExpired:
            # Our subprocess timeout (shouldn't happen with grace period)
            logger.warning(
                f"{req_prefix}Subprocess timeout for {project_name}/{harness}"
            )
            return False
        except Exception as e:
            logger.error(f"{req_prefix}Reproduce error for {project_name}/{harness}: {e}")
            return False
        finally:
            # Clean up temporary file
            testcase_path.unlink(missing_ok=True)
