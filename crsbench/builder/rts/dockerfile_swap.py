"""Dynamically swap Dockerfile FROM line to use RTS base images.

When RTS is enabled, the benchmark Dockerfile's FROM (e.g. base-builder-jvm)
is replaced with a locally-built RTS base image that includes RTS tooling
(JcgEks, OpenClover, helper scripts).  If the RTS image does not exist
locally it is built on the fly from the corresponding Dockerfile under
crsbench/builder/rts/dockerfiles/.
"""

import re
import subprocess
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Local tag for the built RTS base images.
RTS_IMAGE_TAGS = {
    "jvm": "crsbench-rts-jvm:latest",
    "c": "crsbench-rts-c:latest",
    "c++": "crsbench-rts-c:latest",
}

# Dockerfile paths relative to the rts/dockerfiles/ directory.
_RTS_DOCKERFILES = {
    "jvm": "Dockerfile.rts-jvm",
    "c": "Dockerfile.rts-c",
    "c++": "Dockerfile.rts-c",
}

_FROM_RE = re.compile(r"^FROM\s+\S+", re.MULTILINE)

_DOCKERFILES_DIR = Path(__file__).resolve().parent / "dockerfiles"
_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


def _ensure_rts_image(language: str) -> str:
    """Build the RTS base image locally if it doesn't exist. Returns the image tag."""
    tag = RTS_IMAGE_TAGS.get(language)
    if tag is None:
        raise ValueError(f"Unsupported RTS language: {language}")

    # Check if image already exists locally.
    rc = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    if rc.returncode == 0:
        return tag

    dockerfile_name = _RTS_DOCKERFILES.get(language)
    if dockerfile_name is None:
        raise ValueError(f"No RTS Dockerfile for language: {language}")

    dockerfile = _DOCKERFILES_DIR / dockerfile_name
    if not dockerfile.exists():
        raise FileNotFoundError(f"RTS Dockerfile not found: {dockerfile}")

    logger.info(f"Building RTS base image {tag} from {dockerfile} ...")
    result = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            tag,
            "-f",
            str(dockerfile),
            str(_SCRIPTS_DIR.parent),  # context = rts/ dir (so COPY scripts/ works)
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"RTS image build failed:\n{result.stderr}")
        raise RuntimeError(f"Failed to build RTS image {tag}")

    logger.info(f"RTS base image built: {tag}")
    return tag


def swap_dockerfile_from(dockerfile_content: str, language: str) -> str:
    """Replace the first FROM instruction with the RTS base image.

    Ensures the RTS image exists locally (builds if needed).

    Args:
        dockerfile_content: Original Dockerfile content.
        language: Project language from project.yaml (jvm, c, c++).

    Returns:
        Modified Dockerfile content with swapped FROM.

    Raises:
        ValueError: If language is not supported for RTS or no FROM found.
    """
    rts_tag = _ensure_rts_image(language)

    replacement = f"FROM {rts_tag}"
    result, count = _FROM_RE.subn(replacement, dockerfile_content, count=1)
    if count == 0:
        raise ValueError("No FROM instruction found in Dockerfile")
    return result
