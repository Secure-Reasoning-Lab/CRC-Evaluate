"""Container image naming helpers for CRSBench.

Centralizes naming across:
- local CRSBench-managed images
- remote registry images
- OSS-Fuzz helper runtime image names
"""

from __future__ import annotations

DEFAULT_LOCAL_IMAGE_PREFIX = "crsbench"
DEFAULT_REGISTRY = "ghcr.io/team-atlanta/crsbench"
OSS_FUZZ_HELPER_PREFIX = "gcr.io/oss-fuzz"
_SANITIZER_SUFFIX = {
    "address": "asan",
    "undefined": "ubsan",
    "memory": "msan",
    "thread": "tsan",
    "coverage": "cov",
}
_KNOWN_SAN_SUFFIXES = tuple(_SANITIZER_SUFFIX.values())


def sanitizer_project_name(project_name: str, sanitizer: str = "address") -> str:
    """Return a sanitizer-scoped project key."""
    if any(project_name.endswith(f"-{suffix}") for suffix in _KNOWN_SAN_SUFFIXES):
        return project_name
    suffix = _SANITIZER_SUFFIX.get(sanitizer, sanitizer)
    return f"{project_name}-{suffix}"


def local_inc_image_name(
    project_name: str,
    sanitizer: str = "address",
    *,
    local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
) -> str:
    """Return local CRSBench image name for an inc-build image."""
    scoped_project = sanitizer_project_name(project_name, sanitizer)
    return f"{local_prefix}/{scoped_project}:inc"


def registry_inc_image_name(
    project_name: str,
    sanitizer: str = "address",
    *,
    registry: str = DEFAULT_REGISTRY,
) -> str:
    """Return remote registry image name for an inc-build image."""
    scoped_project = sanitizer_project_name(project_name, sanitizer)
    return f"{registry}/{scoped_project}:inc"


def helper_inc_image_name(project_name: str, sanitizer: str = "address") -> str:
    """Return OSS-Fuzz helper-compatible inc image name."""
    scoped_project = sanitizer_project_name(project_name, sanitizer)
    return f"{OSS_FUZZ_HELPER_PREFIX}/{scoped_project}:inc"


def helper_latest_image_name(project_name: str) -> str:
    """Return OSS-Fuzz helper-compatible latest image name."""
    return f"{OSS_FUZZ_HELPER_PREFIX}/{project_name}:latest"
