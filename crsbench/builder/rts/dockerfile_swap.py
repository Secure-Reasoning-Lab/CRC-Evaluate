"""Dynamically swap Dockerfile FROM line to use RTS base images."""

import re

RTS_BASE_IMAGES = {
    "jvm": "ghcr.io/team-atlanta/crsbench-rts-jvm:latest",
    "c": "ghcr.io/team-atlanta/crsbench-rts-c:latest",
    "c++": "ghcr.io/team-atlanta/crsbench-rts-c:latest",
}

_FROM_RE = re.compile(r"^FROM\s+\S+", re.MULTILINE)


def swap_dockerfile_from(dockerfile_content: str, language: str) -> str:
    """Replace the first FROM instruction with the RTS base image.

    Args:
        dockerfile_content: Original Dockerfile content.
        language: Project language from project.yaml (jvm, c, c++).

    Returns:
        Modified Dockerfile content with swapped FROM.

    Raises:
        ValueError: If language is not supported for RTS or no FROM found.
    """
    rts_image = RTS_BASE_IMAGES.get(language)
    if rts_image is None:
        raise ValueError(f"Unsupported RTS language: {language}")

    replacement = f"FROM {rts_image}"
    result, count = _FROM_RE.subn(replacement, dockerfile_content, count=1)
    if count == 0:
        raise ValueError("No FROM instruction found in Dockerfile")
    return result
