"""Process utilities for CRS execution with graceful timeout handling."""

import os
import signal
import subprocess
import threading
import time
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
    stop_event: Optional[threading.Event] = None,
    **kwargs,
) -> Tuple[str, str, int, bool]:
    """Run subprocess with graceful timeout handling and optional early stop.

    When timeout is reached OR stop_event is signaled:
    1. Send SIGTERM for graceful shutdown
    2. Wait `grace_period` seconds for process to exit
    3. Send SIGKILL if still running

    Args:
        cmd: Command and arguments to execute
        timeout: Main timeout in seconds
        grace_period: Seconds to wait after SIGTERM before SIGKILL (default: 60)
        cwd: Working directory
        env: Environment variables
        stop_event: Optional threading.Event to signal early termination
        **kwargs: Additional Popen arguments

    Returns:
        Tuple of (stdout, stderr, returncode, timed_out)
        - stdout: Standard output as string
        - stderr: Standard error as string
        - returncode: Process return code (-9 if killed)
        - timed_out: True if timeout or early stop occurred
    """
    popen_kwargs = dict(kwargs)
    # Fuzzer/container output may contain arbitrary bytes; replace undecodable
    # sequences so timeout handling can complete without crashing.
    popen_kwargs.setdefault("encoding", "utf-8")
    popen_kwargs.setdefault("errors", "replace")
    # GH #182: put the child in its own process group so we can SIGTERM /
    # SIGKILL the whole tree on timeout. Without this, grandchildren (e.g.
    # `docker` CLI wrappers) keep stdout/stderr pipes open and
    # process.communicate() in the grace-period wait can hang forever.
    popen_kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        **popen_kwargs,
    )

    timed_out = False
    early_stopped = False
    stdout = ""
    stderr = ""
    returncode = 0

    if stop_event is None:
        # No early stop - use simple communicate with timeout
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.info(
                f"Timeout reached after {timeout}s, sending SIGTERM for graceful shutdown..."
            )
            stdout, stderr, returncode = _graceful_terminate(process, grace_period)
    else:
        # Poll-based approach to support early stop
        start_time = time.time()
        poll_interval = 1.0  # Check every second

        while True:
            # Check if process completed
            retcode = process.poll()
            if retcode is not None:
                # Process finished normally
                stdout_bytes = process.stdout.read() if process.stdout else ""
                stderr_bytes = process.stderr.read() if process.stderr else ""
                stdout = stdout_bytes if isinstance(stdout_bytes, str) else ""
                stderr = stderr_bytes if isinstance(stderr_bytes, str) else ""
                returncode = retcode
                break

            # Check for timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                timed_out = True
                logger.info(
                    f"Timeout reached after {timeout}s, sending SIGTERM for graceful shutdown..."
                )
                stdout, stderr, returncode = _graceful_terminate(process, grace_period)
                break

            # Check for early stop signal
            if stop_event.is_set():
                early_stopped = True
                timed_out = True  # Report as timed_out for consistent behavior
                logger.info(
                    "Early stop signal received, sending SIGTERM for graceful shutdown..."
                )
                stdout, stderr, returncode = _graceful_terminate(process, grace_period)
                break

            # Sleep briefly before next poll
            time.sleep(poll_interval)

    # Ensure stdout/stderr are strings (handle None case)
    stdout = stdout or ""
    stderr = stderr or ""

    if early_stopped:
        logger.info("Process terminated early via stop signal")

    return stdout, stderr, returncode, timed_out


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    """Send a signal to the child's whole process group, falling back to
    signalling the direct child if the process group is unavailable.

    GH #182: the child is started with ``start_new_session=True`` so its
    PGID equals its PID. Signalling the group ensures grandchildren (e.g.
    ``docker`` CLI wrappers spawned by oss-crs) are reaped too, so they
    do not keep stdout/stderr pipes open during ``communicate()``.
    """
    if process.pid is None or process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        # Child already reaped or inaccessible; fall back to direct signal.
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        # Group already gone. Nothing left to do.
        pass


def _graceful_terminate(
    process: subprocess.Popen, grace_period: int
) -> Tuple[str, str, int]:
    """Gracefully terminate a process with SIGTERM, then SIGKILL if needed.

    Signals the child's whole process group (see ``_signal_process_group``)
    so that grandchildren — notably ``docker`` CLI wrappers spawned by
    oss-crs — are reaped along with the direct child. Without group-level
    signalling, orphan grandchildren hold the pipes open and the
    subsequent ``communicate()`` call can block past the grace period.

    Args:
        process: The subprocess to terminate
        grace_period: Seconds to wait after SIGTERM before SIGKILL

    Returns:
        Tuple of (stdout, stderr, returncode)
    """
    # Graceful shutdown: SIGTERM the whole process group.
    _signal_process_group(process, signal.SIGTERM)

    try:
        # Wait for graceful exit
        stdout, stderr = process.communicate(timeout=grace_period)
        returncode = process.returncode
        logger.info("Process exited gracefully with code {}", returncode)
    except subprocess.TimeoutExpired:
        # Force kill after grace period — SIGKILL the whole group so any
        # lingering grandchildren are reaped before the final wait.
        logger.warning(
            "Process did not exit after {}s grace period, sending SIGKILL...",
            grace_period,
        )
        _signal_process_group(process, signal.SIGKILL)
        stdout, stderr = process.communicate()
        returncode = -9  # SIGKILL
        logger.info("Process killed with SIGKILL")

    return stdout or "", stderr or "", returncode
