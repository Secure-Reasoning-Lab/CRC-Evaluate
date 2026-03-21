"""Docker image utilities for benchmark image lifecycle commands."""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from crsbench.utils.image_names import (
    DEFAULT_LOCAL_IMAGE_PREFIX,
    DEFAULT_REGISTRY,
    helper_inc_image_name,
    local_inc_image_name,
    registry_inc_image_name,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_inc_build_image_name(
    project_name: str,
    sanitizer: str = "address",
    registry: str = DEFAULT_REGISTRY,
) -> str:
    """Get remote registry image name for inc-build."""
    return registry_inc_image_name(project_name, sanitizer, registry=registry)


def get_local_inc_image_name(
    project_name: str,
    sanitizer: str = "address",
    local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
) -> str:
    """Get local CRSBench image name for inc-build."""
    return local_inc_image_name(project_name, sanitizer, local_prefix=local_prefix)


def get_ossfuzz_image_name(project_name: str, sanitizer: str = "address") -> str:
    """Get OSS-Fuzz helper-compatible inc image name."""
    return helper_inc_image_name(project_name, sanitizer)


def docker_image_exists(image_name: str) -> bool:
    """Check if Docker image exists locally."""
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
    """Retag Docker image."""
    try:
        result = subprocess.run(
            ["docker", "tag", src_image, dst_image],
            capture_output=True,
            text=True,
            errors="replace",
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


def docker_pull(image_name: str, timeout: int = 300) -> bool:
    """Pull Docker image from registry."""
    try:
        result = subprocess.run(
            ["docker", "pull", image_name],
            capture_output=True,
            text=True,
            errors="replace",
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


def docker_push(image_name: str, timeout: int = 600) -> bool:
    """Push Docker image to registry."""
    try:
        result = subprocess.run(
            ["docker", "push", image_name],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logger.debug(f"Successfully pushed: {image_name}")
            return True
        logger.debug(f"Failed to push {image_name}: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout pushing {image_name}")
        return False
    except Exception as e:
        logger.error(f"Error pushing {image_name}: {e}")
        return False


def pull_inc_build_image(
    project_name: str,
    sanitizer: str = "address",
    registry: str = DEFAULT_REGISTRY,
    *,
    local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
    timeout: int = 300,
) -> bool:
    """Pull inc-build image and retag for local/helper usage."""
    helper_image = get_ossfuzz_image_name(project_name, sanitizer)
    local_image = get_local_inc_image_name(project_name, sanitizer, local_prefix)

    if docker_image_exists(helper_image):
        return True
    if docker_image_exists(local_image):
        return docker_retag(local_image, helper_image)

    remote = get_inc_build_image_name(project_name, sanitizer, registry)
    if not docker_pull(remote, timeout=timeout):
        return False
    return docker_retag(remote, local_image) and docker_retag(local_image, helper_image)


def push_inc_build_image(
    project_name: str,
    sanitizer: str = "address",
    registry: str = DEFAULT_REGISTRY,
    *,
    local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
    timeout: int = 600,
) -> bool:
    """Push local inc-build image to registry namespace."""
    local_image = get_local_inc_image_name(project_name, sanitizer, local_prefix)
    if not docker_image_exists(local_image):
        logger.warning(f"Local inc-build image missing: {local_image}")
        return False

    remote = get_inc_build_image_name(project_name, sanitizer, registry)
    if not docker_retag(local_image, remote):
        return False
    return docker_push(remote, timeout=timeout)


def get_remote_image_digest(image_name: str) -> Optional[str]:
    """Get remote image digest using docker manifest inspect."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image_name, "--verbose"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)

        if isinstance(data, list):
            for entry in data:
                if "Descriptor" in entry:
                    return entry["Descriptor"].get("digest")
        elif isinstance(data, dict) and "Descriptor" in data:
            return data["Descriptor"].get("digest")
        return None
    except Exception as e:
        logger.debug(f"Error getting remote digest for {image_name}: {e}")
        return None


def get_remote_image_size(image_name: str) -> Optional[int]:
    """Get remote image size (bytes) using docker manifest inspect."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image_name, "--verbose"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)

        if isinstance(data, list):
            total = 0
            for entry in data:
                size = entry.get("Descriptor", {}).get("size")
                if isinstance(size, int):
                    total += size
            return total or None
        if isinstance(data, dict):
            size = data.get("Descriptor", {}).get("size")
            if isinstance(size, int):
                return size
        return None
    except Exception as e:
        logger.debug(f"Error getting remote size for {image_name}: {e}")
        return None


def get_local_image_digest(image_name: str) -> Optional[str]:
    """Get local image digest from RepoDigests."""
    try:
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
            errors="replace",
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split("\n"):
            if line and "@sha256:" in line:
                return line.split("@")[1]
        return None
    except Exception as e:
        logger.debug(f"Error getting local digest for {image_name}: {e}")
        return None


def get_local_image_id(image_name: str) -> Optional[str]:
    """Get local image ID."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception as e:
        logger.debug(f"Error getting image ID for {image_name}: {e}")
        return None
