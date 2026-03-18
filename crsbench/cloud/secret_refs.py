"""Strict resolution helpers for cloud secret-bearing config fields."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_ENV_REF_PATTERN = re.compile(r"^os\.environ/([A-Za-z_][A-Za-z0-9_]*)$")


class CloudSecretReferenceError(ValueError):
    """Field-scoped error raised for invalid secret references."""

    def __init__(self, *, field_path: str, detail: str) -> None:
        self.field_path = field_path
        self.detail = detail
        super().__init__(f"{field_path}: {detail}")


@dataclass(frozen=True)
class _Reference:
    kind: str
    value: str


def resolve_secret_text(
    value: str | None,
    *,
    field_path: str,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> str | None:
    """Resolve a secret-bearing text field from literal, env, or file reference."""
    if value is None:
        return None

    reference = _parse_reference(value, field_path=field_path)
    if reference.kind == "literal":
        return _require_non_empty(reference.value, field_path=field_path)
    if reference.kind == "env":
        resolved = _lookup_env(reference.value, field_path=field_path, env=env)
        return _require_non_empty(
            resolved,
            field_path=field_path,
            empty_detail=f"env var {reference.value} resolved to an empty value",
        )
    if reference.kind == "file":
        path = _resolve_existing_path(
            reference.value,
            field_path=field_path,
            cwd=cwd,
        )
        return _require_non_empty(
            path.read_text(encoding="utf-8"), field_path=field_path
        )
    raise AssertionError(f"Unsupported reference kind: {reference.kind}")


def resolve_secret_path(
    value: str | None,
    *,
    field_path: str,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> str | None:
    """Resolve a path-bearing field from literal, env, or explicit file path ref."""
    if value is None:
        return None

    reference = _parse_reference(value, field_path=field_path)
    if reference.kind == "literal":
        path_value = _require_non_empty(reference.value, field_path=field_path)
    elif reference.kind == "env":
        path_value = _lookup_env(reference.value, field_path=field_path, env=env)
        path_value = _require_non_empty(
            path_value,
            field_path=field_path,
            empty_detail=f"env var {reference.value} resolved to an empty value",
        )
    elif reference.kind == "file":
        path_value = _require_non_empty(reference.value, field_path=field_path)
    else:
        raise AssertionError(f"Unsupported reference kind: {reference.kind}")

    return str(_resolve_existing_path(path_value, field_path=field_path, cwd=cwd))


def validate_secret_reference_format(value: str, *, field_path: str) -> str:
    """Validate secret-reference syntax without resolving env vars or files."""
    reference = _parse_reference(value, field_path=field_path)
    if reference.kind == "literal":
        return _require_non_empty(reference.value, field_path=field_path)
    if reference.kind == "env":
        return f"os.environ/{reference.value}"
    if reference.kind == "file":
        return f"file:{_require_non_empty(reference.value, field_path=field_path)}"
    raise AssertionError(f"Unsupported reference kind: {reference.kind}")


def _parse_reference(value: str, *, field_path: str) -> _Reference:
    text = value.strip()
    if not text:
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail="value must not be empty",
        )

    env_match = _ENV_REF_PATTERN.fullmatch(text)
    if env_match is not None:
        return _Reference(kind="env", value=env_match.group(1))

    if "os.environ/" in text:
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail="invalid os.environ reference",
        )

    if text.startswith("file:"):
        path_value = text.removeprefix("file:").strip()
        if not path_value:
            raise CloudSecretReferenceError(
                field_path=field_path,
                detail="invalid file reference",
            )
        return _Reference(kind="file", value=path_value)

    return _Reference(kind="literal", value=text)


def _lookup_env(
    name: str,
    *,
    field_path: str,
    env: Mapping[str, str] | None,
) -> str:
    source = os.environ if env is None else env
    if name not in source:
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail=f"env var {name} is not set",
        )
    return source[name]


def _require_non_empty(
    value: str,
    *,
    field_path: str,
    empty_detail: str = "resolved value must not be empty",
) -> str:
    resolved = value.strip()
    if not resolved:
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail=empty_detail,
        )
    return resolved


def _resolve_existing_path(
    value: str,
    *,
    field_path: str,
    cwd: Path | str | None,
) -> Path:
    base = Path.cwd() if cwd is None else Path(cwd)
    path = Path(value)
    if not path.is_absolute():
        path = base / path

    if not path.exists():
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail=f"file {value} does not exist",
        )
    if not path.is_file():
        raise CloudSecretReferenceError(
            field_path=field_path,
            detail=f"file {value} is not a regular file",
        )
    return path.resolve()
