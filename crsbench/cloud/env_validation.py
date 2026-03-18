"""Shared validation helpers for cloud-provided environment variables."""

from __future__ import annotations

import re

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RESERVED_REMOTE_ENV_VARS = frozenset(
    {
        "CRSBENCH_CLOUD_EXPERIMENT",
        "CRSBENCH_CLOUD_INSTANCE_ID",
        "CRSBENCH_CLOUD_INSTANCE_NAME",
        "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS",
        "CRSBENCH_CLOUD_ZONE",
        "CRSBENCH_EXPERIMENT_NAME",
        "CRSBENCH_LOG_LEVEL",
        "CRSBENCH_REDIS_HOST",
        "CRSBENCH_REDIS_PASSWORD",
        "CRSBENCH_WORKER_CORES_PER_JOB",
        "CRSBENCH_WORKER_CPU_TAG",
        "CRSBENCH_WORKER_JOBS",
        "CRSBENCH_WORKER_NAME",
    }
)


def normalize_env_name(name: str, *, field_path: str) -> str:
    """Return a trimmed env name or raise if it is malformed or reserved."""
    normalized = name.strip()
    if not normalized:
        raise ValueError(f"{field_path} must not contain blank environment names")
    if not _ENV_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_path} contains invalid environment name {normalized!r}"
        )
    if normalized in RESERVED_REMOTE_ENV_VARS:
        raise ValueError(
            f"{field_path} must not include runtime-managed environment variable "
            f"{normalized}"
        )
    return normalized
