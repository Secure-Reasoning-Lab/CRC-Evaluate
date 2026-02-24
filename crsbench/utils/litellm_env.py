"""Canonical LiteLLM runtime environment resolution for CRSBench."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _first_non_empty(
    env: Mapping[str, str], *keys: str, default: str | None = None
) -> str | None:
    for key in keys:
        value = _clean(env.get(key))
        if value is not None:
            return value
    return default


@dataclass(frozen=True)
class LiteLLMRuntimeEnv:
    """Resolved LiteLLM runtime inputs from canonical CRSBench env vars."""

    mode: str
    base_url: str | None
    upstream_base_url: str | None
    api_key: str | None
    master_key: str | None

    @property
    def direct_base_url(self) -> str | None:
        """Immediate endpoint called by CRS runtime."""
        if self.mode == "external":
            return self.upstream_base_url or self.base_url
        return self.base_url

    @property
    def tracking_base_url(self) -> str | None:
        """Endpoint used by CRSBench tracking control plane APIs."""
        if self.mode == "external":
            return self.direct_base_url
        return self.base_url


def resolve_litellm_runtime_env(
    mode: str, environ: Mapping[str, str] | None = None
) -> LiteLLMRuntimeEnv:
    """Resolve canonical CRSBench LiteLLM env vars."""
    env = environ or os.environ
    return LiteLLMRuntimeEnv(
        mode=mode,
        base_url=_first_non_empty(
            env,
            "CRSBENCH_LLM_BASE_URL",
        ),
        upstream_base_url=_first_non_empty(
            env,
            "CRSBENCH_LLM_UPSTREAM_BASE_URL",
        ),
        api_key=_first_non_empty(
            env,
            "CRSBENCH_LLM_UPSTREAM_API_KEY",
        ),
        master_key=_first_non_empty(
            env,
            "CRSBENCH_LLM_UPSTREAM_MASTER_KEY",
            "CRSBENCH_LLM_MASTER_KEY",
        ),
    )


def required_env_errors_for_mode(
    runtime_env: LiteLLMRuntimeEnv, *, tracking_enabled: bool
) -> list[str]:
    """Return canonical contract violations for configured LiteLLM mode."""
    errors: list[str] = []
    mode = runtime_env.mode

    if mode == "external":
        if runtime_env.direct_base_url is None:
            errors.append(
                "CRSBENCH_LLM_UPSTREAM_BASE_URL (preferred for external BASE endpoint) "
                "or CRSBENCH_LLM_BASE_URL must be set"
            )
        if runtime_env.api_key is None and runtime_env.master_key is None:
            errors.append(
                "CRSBENCH_LLM_UPSTREAM_API_KEY "
                "or CRSBENCH_LLM_UPSTREAM_MASTER_KEY or CRSBENCH_LLM_MASTER_KEY must be set"
            )
    elif mode == "self_hosted":
        errors.append(
            "litellm_mode='self_hosted' is not implemented yet; use litellm_mode='external'"
        )

    if tracking_enabled:
        if runtime_env.tracking_base_url is None:
            errors.append(
                "Tracking requires CRSBENCH_LLM_BASE_URL semantics to resolve"
            )
        if runtime_env.master_key is None:
            errors.append(
                "Tracking requires CRSBENCH_LLM_UPSTREAM_MASTER_KEY or CRSBENCH_LLM_MASTER_KEY"
            )

    return errors
