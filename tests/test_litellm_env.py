"""Tests for canonical LiteLLM runtime env resolution."""

from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)


def test_external_prefers_canonical_upstream_base() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "external",
        {
            "LITELLM_UPSTREAM_BASE_URL": "http://central:4000",
            "CRSBENCH_LLM_BASE_URL": "http://local:4000",
            "LITELLM_UPSTREAM_API_KEY": "sk-api",
        },
    )
    assert runtime_env.direct_base_url == "http://central:4000"
    assert required_env_errors_for_mode(runtime_env, tracking_enabled=False) == []


def test_internal_does_not_require_external_runtime_variables() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "internal",
        {},
    )
    assert required_env_errors_for_mode(runtime_env, tracking_enabled=True) == []


def test_explicit_empty_environment_does_not_read_process_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LITELLM_UPSTREAM_BASE_URL", "http://host:4000")
    runtime_env = resolve_litellm_runtime_env("external", {})
    assert runtime_env.direct_base_url is None


def test_legacy_fallback_vars_do_not_resolve() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "external",
        {
            "OLD_CRSBENCH_LLM_BASE_URL": "http://legacy-upstream:4000",
            "OLD_CRSBENCH_LLM_API_KEY": "legacy-key",
        },
    )
    assert runtime_env.direct_base_url is None
    assert runtime_env.api_key is None
