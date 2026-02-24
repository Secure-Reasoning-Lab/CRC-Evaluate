"""Tests for canonical LiteLLM runtime env resolution."""

from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)


def test_external_prefers_canonical_upstream_base() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "external",
        {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "http://central:4000",
            "CRSBENCH_LLM_BASE_URL": "http://local:4000",
            "CRSBENCH_LLM_UPSTREAM_API_KEY": "sk-api",
        },
    )
    assert runtime_env.direct_base_url == "http://central:4000"
    assert required_env_errors_for_mode(runtime_env, tracking_enabled=False) == []


def test_self_hosted_reports_not_implemented() -> None:
    runtime_env = resolve_litellm_runtime_env(
        "self_hosted",
        {
            "CRSBENCH_LLM_BASE_URL": "http://127.0.0.1:4000",
        },
    )
    errors = required_env_errors_for_mode(runtime_env, tracking_enabled=False)
    assert (
        "litellm_mode='self_hosted' is not implemented yet; use litellm_mode='external'"
        in errors
    )


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
