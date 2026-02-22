"""Tests for canonical LiteLLM runtime env resolution."""

from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)


def test_passthrough_prefers_canonical_upstream_base() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "passthrough",
        {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "http://central:4000",
            "CRSBENCH_LLM_BASE_URL": "http://local:4000",
            "CRSBENCH_LLM_API_KEY": "sk-api",
        },
    )
    assert runtime_env.direct_base_url == "http://central:4000"
    assert required_env_errors_for_mode(runtime_env, tracking_enabled=False) == []


def test_proxy_requires_base_and_master() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "proxy",
        {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "http://central:4000",
            "CRSBENCH_LLM_API_KEY": "sk-api",
        },
    )
    errors = required_env_errors_for_mode(runtime_env, tracking_enabled=False)
    assert "CRSBENCH_LLM_BASE_URL must be set" in errors
    assert "CRSBENCH_LLM_MASTER_KEY must be set" in errors


def test_legacy_fallback_vars_do_not_resolve() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "passthrough",
        {
            "OLD_CRSBENCH_LLM_BASE_URL": "http://legacy-upstream:4000",
            "OLD_CRSBENCH_LLM_API_KEY": "legacy-key",
        },
    )
    assert runtime_env.direct_base_url is None
    assert runtime_env.api_key is None
