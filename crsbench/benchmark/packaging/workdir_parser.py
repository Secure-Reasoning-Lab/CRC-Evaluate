"""Parse WORKDIR from Dockerfile to determine expected source directory name.

OSS-Fuzz convention: Base image sets WORKDIR /src, so both absolute and relative
patterns resolve to /src/{name}:
- WORKDIR $SRC/curl → /src/curl → "curl"
- WORKDIR libtiff → /src/libtiff → "libtiff"
"""

import re
from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_expected_source_dir(dockerfile_path: Path) -> Optional[str]:
    """Extract source directory name from last WORKDIR directive.

    Args:
        dockerfile_path: Path to Dockerfile

    Returns:
        Directory name (e.g., "curl", "libtiff"), or None if not found
    """
    if not dockerfile_path.exists():
        logger.error(f"Dockerfile not found: {dockerfile_path}")
        return None

    lines = dockerfile_path.read_text().splitlines()

    for line in reversed(lines):
        # Skip comments and empty lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        match = re.match(r"WORKDIR\s+(.+)", stripped, re.IGNORECASE)
        if match:
            workdir = match.group(1).strip()
            return _normalize_workdir(workdir)

    logger.warning(f"No WORKDIR found in {dockerfile_path}")
    return None


def _normalize_workdir(workdir: str) -> str:
    """Normalize WORKDIR path to extract directory name.

    Handles:
    - $SRC/curl → curl
    - /src/curl → curl
    - ${SRC}/curl → curl
    - libtiff → libtiff (relative)
    - /src/project-name → project-name

    Args:
        workdir: Raw WORKDIR value from Dockerfile

    Returns:
        Directory name
    """
    # Remove common prefixes
    normalized = workdir
    for prefix in ["$SRC/", "${SRC}/", "/src/"]:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break

    # Return the last path component (handles nested paths)
    return Path(normalized).name
