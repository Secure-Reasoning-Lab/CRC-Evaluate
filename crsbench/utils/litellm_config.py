"""Validation helpers for transported internal LiteLLM configurations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

_ENV_REFERENCE = re.compile(r"os\.environ/[A-Za-z_][A-Za-z0-9_]*")
_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "api_token",
        "aws_secret_access_key",
        "azure_ad_token",
        "client_secret",
        "credentials",
        "master_key",
        "password",
        "token",
        "vertex_credentials",
    }
)
_SENSITIVE_HEADER_FIELDS = frozenset(
    {"api-key", "authorization", "proxy-authorization", "x-api-key"}
)
_HEADER_MAPPINGS = frozenset({"default_headers", "extra_headers", "headers"})
_SENSITIVE_FIELD_SUFFIXES = (
    "_api_key",
    "_credentials",
    "_password",
    "_secret",
    "_secret_key",
    "_token",
)


def _is_sensitive_field_name(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized.endswith("cost_per_token"):
        return False
    return normalized in _SENSITIVE_FIELDS or normalized.endswith(
        _SENSITIVE_FIELD_SUFFIXES
    )


def _requires_environment_reference(key: str, path: tuple[str, ...]) -> bool:
    normalized = key.lower().replace("_", "-")
    if _is_sensitive_field_name(key):
        return True
    parent = path[-1].lower() if path else ""
    return parent in _HEADER_MAPPINGS and normalized in _SENSITIVE_HEADER_FIELDS


def _validate_url_credentials(value: Any, path: tuple[str, ...]) -> None:
    if not isinstance(value, str):
        return
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"Internal LiteLLM URL field '{'.'.join(path)}' must not contain credentials"
        )
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_sensitive_field_name(key) and query_value:
            raise ValueError(
                f"Internal LiteLLM URL field '{'.'.join(path)}' must not contain credentials"
            )


def _validate_secret_references(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, str) and _ENV_REFERENCE.fullmatch(value) is not None:
        supported_location = (
            len(path) == 4
            and path[0] == "model_list"
            and path[1].isdigit()
            and path[2] == "litellm_params"
        )
        if not supported_location:
            raise ValueError(
                "OSS-CRS internal LiteLLM environment references must be scalar "
                "model_list[].litellm_params values"
            )
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = (*path, key)
            if _requires_environment_reference(key, path) and item is not None:
                if not isinstance(item, str) or _ENV_REFERENCE.fullmatch(item) is None:
                    field_path = ".".join(item_path)
                    raise ValueError(
                        f"Internal LiteLLM secret field '{field_path}' must use os.environ/NAME"
                    )
            _validate_url_credentials(item, item_path)
            _validate_secret_references(item, item_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_secret_references(item, (*path, str(index)))


def parse_internal_litellm_config(content: str | bytes) -> dict[str, Any]:
    """Parse a transportable internal LiteLLM YAML mapping and reject embedded credentials."""
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid internal LiteLLM YAML configuration") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Internal LiteLLM config must be a YAML mapping")
    _validate_secret_references(parsed)
    return parsed


def normalize_internal_litellm_config(content: str | bytes) -> str:
    """Return validated internal LiteLLM YAML without comments or transport-only formatting."""
    parsed = parse_internal_litellm_config(content)
    return yaml.safe_dump(parsed, sort_keys=False)


def internal_litellm_config_path(value: Any) -> str | None:
    """Return the internal LiteLLM path from an experiment configuration mapping."""
    if not isinstance(value, dict):
        return None
    runtime = value.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    litellm = runtime.get("litellm")
    litellm = litellm if isinstance(litellm, dict) else {}
    skip_values = (
        litellm.get("skip"),
        runtime.get("skip_litellm"),
        value.get("skip_litellm"),
    )
    if any(
        item is True
        or (
            isinstance(item, str) and item.strip().lower() in {"1", "true", "yes", "on"}
        )
        for item in skip_values
    ):
        return None
    mode = litellm.get("mode", runtime.get("litellm_mode", value.get("litellm_mode")))
    if mode != "internal":
        return None
    crs_compose = value.get("crs_compose")
    if not isinstance(crs_compose, dict):
        return None
    path = crs_compose.get("litellm_config_path")
    return path if isinstance(path, str) and path.strip() else None


def read_internal_litellm_config_snapshot(
    experiment_config_path: str | Path,
) -> bytes | None:
    """Read and normalize the internal LiteLLM configuration selected by an experiment file."""
    experiment_path = Path(experiment_config_path)
    if not experiment_path.is_file():
        return None
    raw_config = yaml.safe_load(experiment_path.read_text(encoding="utf-8"))
    configured_path = internal_litellm_config_path(raw_config)
    if configured_path is None:
        return None
    litellm_path = Path(configured_path).expanduser()
    if not litellm_path.is_absolute():
        cwd_path = litellm_path.resolve()
        litellm_path = (
            cwd_path if cwd_path.is_file() else experiment_path.parent / litellm_path
        )
    litellm_path = litellm_path.resolve()
    if not litellm_path.is_file():
        raise FileNotFoundError(
            f"Internal LiteLLM config file not found: {litellm_path}"
        )
    return normalize_internal_litellm_config(litellm_path.read_bytes()).encode("utf-8")
