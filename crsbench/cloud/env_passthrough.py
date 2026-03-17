"""Operator-side validation and resolution for remote VM environment passthrough."""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


class CloudEnvPassthroughError(ValueError):
    """Raised when configured remote environment passthrough cannot be resolved."""


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


def normalize_env_name_list(values: Sequence[str], *, field_path: str) -> list[str]:
    """Normalize one configured env-var name list with stable de-duplication."""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        env_name = normalize_env_name(str(item), field_path=field_path)
        if env_name in seen:
            continue
        seen.add(env_name)
        normalized.append(env_name)
    return normalized


def resolve_env_passthrough(
    names: Sequence[str],
    *,
    field_path: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve configured env vars from the operator environment."""
    source = os.environ if env is None else env
    resolved: OrderedDict[str, str] = OrderedDict()
    for env_name in names:
        value = source.get(env_name)
        if value is None:
            raise CloudEnvPassthroughError(
                f"{field_path} requires environment variable {env_name} to be set"
            )
        if not value:
            raise CloudEnvPassthroughError(
                f"{field_path} requires environment variable {env_name} to be non-empty"
            )
        resolved[env_name] = value
    return dict(resolved)


def merge_env_passthrough(*groups: Sequence[str]) -> list[str]:
    """Merge multiple env-name lists while preserving first occurrence order."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for env_name in group:
            if env_name in seen:
                continue
            seen.add(env_name)
            merged.append(env_name)
    return merged
