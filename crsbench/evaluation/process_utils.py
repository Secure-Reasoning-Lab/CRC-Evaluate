"""Process utilities for CRS execution with graceful timeout handling."""

import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def run_with_graceful_timeout(
    cmd: list,
    timeout: int,
    grace_period: int = 60,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    **kwargs,
) -> Tuple[str, str, int, bool]:
    """Run subprocess with graceful timeout handling.

    When timeout is reached:
    1. Send SIGTERM for graceful shutdown
    2. Wait `grace_period` seconds for process to exit
    3. Send SIGKILL if still running

    Args:
        cmd: Command and arguments to execute
        timeout: Main timeout in seconds
        grace_period: Seconds to wait after SIGTERM before SIGKILL (default: 60)
        cwd: Working directory
        env: Environment variables
        **kwargs: Additional Popen arguments

    Returns:
        Tuple of (stdout, stderr, returncode, timed_out)
        - stdout: Standard output as string
        - stderr: Standard error as string
        - returncode: Process return code (-9 if killed)
        - timed_out: True if timeout occurred
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        **kwargs,
    )

    timed_out = False

    try:
        # Normal execution
        stdout, stderr = process.communicate(timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.info(
            f"Timeout reached after {timeout}s, sending SIGTERM for graceful shutdown..."
        )

        # Graceful shutdown: send SIGTERM
        process.terminate()

        try:
            # Wait for graceful exit
            stdout, stderr = process.communicate(timeout=grace_period)
            returncode = process.returncode
            logger.info(f"Process exited gracefully with code {returncode}")
        except subprocess.TimeoutExpired:
            # Force kill after grace period
            logger.warning(
                f"Process did not exit after {grace_period}s grace period, sending SIGKILL..."
            )
            process.kill()
            stdout, stderr = process.communicate()
            returncode = -9  # SIGKILL
            logger.info("Process killed with SIGKILL")

    # Ensure stdout/stderr are strings (handle None case)
    stdout = stdout or ""
    stderr = stderr or ""

    return stdout, stderr, returncode, timed_out
