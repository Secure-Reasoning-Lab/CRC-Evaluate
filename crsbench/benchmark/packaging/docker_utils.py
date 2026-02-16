"""Docker image utilities for benchmark packaging.

Provides standalone utilities for Docker image operations without
requiring OSS-Fuzz infrastructure (no helper.py dependency).
"""

import subprocess
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_REGISTRY = "ghcr.io/team-atlanta/crsbench"


def get_inc_build_image_name(
    project_name: str,
    sanitizer: str = "address",
    registry: str = DEFAULT_REGISTRY,
) -> str:
    """Get registry image name for inc-build.

    Args:
        project_name: OSS-Fuzz project name
        sanitizer: Sanitizer type (address, memory, undefined)
        registry: Docker registry

    Returns:
        Full image name (e.g., ghcr.io/team-atlanta/crsbench/project:inc-address)
    """
    return f"{registry}/{project_name}:inc-{sanitizer}"


def get_ossfuzz_image_name(project_name: str, sanitizer: str = "address") -> str:
    """Get OSS-Fuzz compatible image name.

    OSS-Fuzz helper.py expects images in format: aixcc-afc/{project}:{tag}

    Args:
        project_name: OSS-Fuzz project name
        sanitizer: Sanitizer type

    Returns:
        OSS-Fuzz compatible image name (e.g., aixcc-afc/project:inc-address)
    """
    return f"aixcc-afc/{project_name}:inc-{sanitizer}"


def docker_image_exists(image_name: str) -> bool:
    """Check if Docker image exists locally.

    Args:
        image_name: Full Docker image name

    Returns:
        True if image exists locally
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception as e:
        logger.debug(f"Error checking image {image_name}: {e}")
        return False


def docker_retag(src_image: str, dst_image: str) -> bool:
    """Retag Docker image.

    Args:
        src_image: Source image name
        dst_image: Destination image name

    Returns:
        True if retag succeeded
    """
    try:
        result = subprocess.run(
            ["docker", "tag", src_image, dst_image],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logger.debug(f"Retagged {src_image} -> {dst_image}")
            return True
        logger.error(f"Failed to retag: {result.stderr}")
        return False
    except Exception as e:
        logger.error(f"Error retagging image: {e}")
        return False


def docker_pull(image_name: str, timeout: int = 1200) -> bool:
    """Pull Docker image from registry.

    Args:
        image_name: Full Docker image name
        timeout: Timeout in seconds (default: 1200 = 20 minutes)

    Returns:
        True if pull succeeded
    """
    try:
        result = subprocess.run(
            ["docker", "pull", image_name],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logger.debug(f"Successfully pulled: {image_name}")
            return True
        logger.debug(f"Failed to pull {image_name}: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout pulling {image_name}")
        return False
    except Exception as e:
        logger.error(f"Error pulling {image_name}: {e}")
        return False


def pull_inc_build_image(
    project_name: str,
    sanitizer: str = "address",
    registry: str = DEFAULT_REGISTRY,
) -> bool:
    """Pull inc-build image and retag for OSS-Fuzz.

    Args:
        project_name: OSS-Fuzz project name
        sanitizer: Sanitizer type
        registry: Docker registry

    Returns:
        True if image is available (pulled or already local)
    """
    ossfuzz_image = get_ossfuzz_image_name(project_name, sanitizer)

    # Check if already available in OSS-Fuzz format
    if docker_image_exists(ossfuzz_image):
        logger.debug(f"OSS-Fuzz compatible image already exists: {ossfuzz_image}")
        return True

    inc_image = get_inc_build_image_name(project_name, sanitizer, registry)

    # Check if source image exists locally
    if docker_image_exists(inc_image):
        logger.debug(f"Inc-build image available locally: {inc_image}")
        return docker_retag(inc_image, ossfuzz_image)

    # Pull from registry
    logger.debug(f"Pulling inc-build image: {inc_image}")
    if docker_pull(inc_image):
        return docker_retag(inc_image, ossfuzz_image)

    return False


def get_remote_image_digest(image_name: str) -> Optional[str]:
    """Get remote image digest using docker manifest inspect.

    Args:
        image_name: Full Docker image name

    Returns:
        Digest string (e.g., sha256:abc...) or None if not available
    """
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image_name, "--verbose"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None

        # Parse the JSON output to get digest
        import json

        data = json.loads(result.stdout)

        # Handle different manifest formats
        if isinstance(data, list):
            # Multi-platform manifest list
            for entry in data:
                if "Descriptor" in entry:
                    return entry["Descriptor"].get("digest")
        elif isinstance(data, dict):
            # Single manifest
            if "Descriptor" in data:
                return data["Descriptor"].get("digest")

        return None
    except Exception as e:
        logger.debug(f"Error getting remote digest for {image_name}: {e}")
        return None


def get_local_image_digest(image_name: str) -> Optional[str]:
    """Get local image digest from RepoDigests.

    Args:
        image_name: Docker image name (OSS-Fuzz format)

    Returns:
        Digest string (e.g., sha256:abc...) or None if not available
    """
    try:
        # Get the image inspect data
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image_name,
                "--format",
                '{{range .RepoDigests}}{{.}}{{"\\n"}}{{end}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None

        # Parse RepoDigests to find matching registry
        for line in result.stdout.strip().split("\n"):
            if line and "@sha256:" in line:
                # Extract digest from "registry/image@sha256:..."
                return line.split("@")[1]

        return None
    except Exception as e:
        logger.debug(f"Error getting local digest for {image_name}: {e}")
        return None


def get_local_image_id(image_name: str) -> Optional[str]:
    """Get local image ID.

    Args:
        image_name: Docker image name

    Returns:
        Image ID string or None if not available
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception as e:
        logger.debug(f"Error getting image ID for {image_name}: {e}")
        return None
