"""Docker utilities for CRSBench.

This module provides utilities for working with Docker containers,
particularly for fixing file ownership issues from Docker builds.
"""

import os
import subprocess
from pathlib import Path

from crsbench.utils.logger import get_logger
from crsbench.utils.subprocess_utils import run_with_timeout

logger = get_logger(__name__)


def docker_rmtree(path: Path, *, timeout: int = 120) -> bool:
    """Remove a directory that may contain root-owned files from Docker.

    Tries shutil.rmtree first (fast, works if ownership already fixed).
    Falls back to Docker rm -rf if permission errors occur.

    Args:
        path: Directory to remove
        timeout: Timeout in seconds for the Docker fallback (default: 120s)

    Returns:
        True if successfully removed, False otherwise
    """
    import shutil

    if not path.exists():
        return True

    # Fast path: files are already user-owned (post-build ownership was fixed)
    try:
        shutil.rmtree(path)
        logger.debug(f"Removed {path}")
        return True
    except PermissionError:
        pass

    # Fallback: root-owned files from a failed/interrupted build
    # Use "sh -c" to remove contents without removing the mount point itself.
    # "rm -rf /target" fails with "Resource busy" because /target is a bind mount.
    path = path.resolve()
    try:
        result = run_with_timeout(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{path}:/target",
                "alpine",
                "sh",
                "-c",
                "rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null; true",
            ],
            timeout=timeout,
        )

        if result.returncode == 0:
            logger.debug(f"Removed contents of {path} via Docker")
            # Remove the now-empty directory on the host
            try:
                path.rmdir()
            except OSError:
                pass  # OK if dir isn't fully empty or is still mounted
            return True

        logger.warning(f"Failed to remove {path} via Docker: {result.stderr}")
        return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout removing {path} via Docker")
        return False
    except Exception as e:
        logger.warning(f"Could not remove {path}: {e}")
        return False


def fix_docker_ownership(path: Path, *, timeout: int = 300) -> bool:
    """Fix ownership of Docker-created files.

    Docker builds run as root, creating files owned by root.
    This function changes ownership to the current user using
    a lightweight Alpine container (no sudo required).

    Args:
        path: Path to fix ownership for (file or directory)
        timeout: Timeout in seconds for the Docker command (default: 300s
            to handle large projects like wireshark)

    Returns:
        True if successful, False otherwise
    """
    if not path.exists():
        return True  # Nothing to fix

    path = path.resolve()
    uid = os.getuid()
    gid = os.getgid()

    try:
        result = run_with_timeout(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{path}:/target",
                "alpine",
                "chown",
                "-R",
                f"{uid}:{gid}",
                "/target",
            ],
            timeout=timeout,
        )

        if result.returncode == 0:
            logger.debug(f"Fixed ownership of {path}")
            return True

        logger.warning(f"Failed to fix ownership of {path}: {result.stderr}")
        return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout fixing ownership of {path}")
        return False
    except Exception as e:
        logger.debug(f"Could not fix ownership of {path}: {e}")
        return False
