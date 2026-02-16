"""Subprocess utilities with process-group-aware timeout handling.

When subprocess.run(timeout=X) fires, Python sends SIGKILL to the direct child
only. If that child spawned grandchildren (e.g., docker run), those survive
because Docker manages them via the Docker daemon. The surviving grandchild
keeps stdout/stderr pipes open, causing process.communicate() to block forever.

This module provides run_with_timeout() which uses start_new_session=True to
create a new process group, then kills the entire group on timeout via
os.killpg(). This ensures all descendants (including docker run) are terminated.
"""

import os
import signal
import subprocess
from pathlib import Path
from typing import Optional, Union

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def run_with_timeout(
    cmd: list[str],
    timeout: int,
    cwd: Optional[Union[str, Path]] = None,
    *,
    capture_output: bool = True,
    text: bool = True,
    stdin: Optional[int] = subprocess.DEVNULL,
) -> subprocess.CompletedProcess:
    """Run a command with timeout that kills the entire process group.

    Unlike subprocess.run(timeout=X) which only kills the direct child,
    this function creates a new process group and kills all processes in
    the group on timeout. This prevents orphaned Docker containers from
    keeping pipes open and blocking communicate() forever.

    Args:
        cmd: Command and arguments to run.
        timeout: Timeout in seconds.
        cwd: Working directory for the command.
        capture_output: If True, capture stdout and stderr via PIPE.
        text: If True, decode stdout/stderr as text. If False, return bytes.
        stdin: File descriptor for stdin. Defaults to DEVNULL.

    Returns:
        subprocess.CompletedProcess with stdout, stderr, and returncode.

    Raises:
        subprocess.TimeoutExpired: If the command times out. The stdout and
            stderr attributes of the exception contain any captured output.
    """
    stdout_arg = subprocess.PIPE if capture_output else None
    stderr_arg = subprocess.PIPE if capture_output else None

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=stdin,
        stdout=stdout_arg,
        stderr=stderr_arg,
        text=text,
        start_new_session=True,
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        # Now safe to communicate -- all children in the group are dead
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        ) from None


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill the entire process group for a Popen process.

    Sends SIGTERM first for graceful shutdown, then SIGKILL if the process
    group is still alive after 5 seconds.

    Args:
        process: The Popen process whose group should be killed.
    """
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        # Process already exited or we lack permission
        return

    # SIGTERM for graceful shutdown (gives Docker containers time to stop)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    # Wait briefly for graceful exit.
    # Note: process.wait() only checks the direct child (helper.py), which
    # exits almost instantly from SIGTERM. But `docker run` (grandchild) does
    # NOT exit from SIGTERM -- it forwards the signal to the container and
    # waits for it to stop. So we must always proceed to SIGKILL regardless
    # of whether the direct child has exited.
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    # SIGKILL the entire process group. This is always needed because
    # `docker run` CLI survives SIGTERM (it waits for the container to stop,
    # keeping stdout/stderr pipes open and blocking communicate()).
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    # Final fallback: kill the direct process if killpg missed it
    try:
        process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def docker_kill_orphans(container_prefix: str) -> int:
    """Kill orphaned Docker containers matching a name prefix.

    This is a belt-and-suspenders safety net for edge cases where killpg
    misses containers (e.g., Docker daemon managing container lifecycle
    independently). Not called automatically; available for explicit cleanup.

    Args:
        container_prefix: Prefix to match against container names.

    Returns:
        Number of containers killed.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={container_prefix}", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        container_ids = result.stdout.strip().splitlines()
        if not container_ids or container_ids == [""]:
            return 0

        killed = 0
        for cid in container_ids:
            try:
                subprocess.run(
                    ["docker", "kill", cid],
                    capture_output=True,
                    timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                killed += 1
                logger.debug(f"Killed orphaned container: {cid}")
            except (subprocess.TimeoutExpired, Exception):
                logger.warning(f"Failed to kill container: {cid}")

        return killed
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.warning(f"Failed to list orphaned containers: {e}")
        return 0
