"""Docker utilities for CRSBench.

This module provides utilities for working with Docker containers,
particularly for fixing file ownership issues from Docker builds.
"""

import os
import subprocess
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def fix_docker_ownership(path: Path, *, timeout: int = 60) -> bool:
    """Fix ownership of Docker-created files.

    Docker builds run as root, creating files owned by root.
    This function changes ownership to the current user using
    a lightweight Alpine container (no sudo required).

    Args:
        path: Path to fix ownership for (file or directory)
        timeout: Timeout in seconds for the Docker command

    Returns:
        True if successful, False otherwise
    """
    if not path.exists():
        return True  # Nothing to fix

    path = path.resolve()
    uid = os.getuid()
    gid = os.getgid()

    try:
        result = subprocess.run(
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
            capture_output=True,
            text=True,
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
