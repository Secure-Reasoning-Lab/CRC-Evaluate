"""Tests for internal LiteLLM configuration transport validation."""

import pytest
from crsbench.utils.litellm_config import (
    normalize_internal_litellm_config,
    parse_internal_litellm_config,
)


def test_internal_litellm_config_accepts_environment_secret_references():
    parsed = parse_internal_litellm_config(
        """
model_list:
  - model_name: claude-opus
    litellm_params:
      model: openai/gpt
      api_base: os.environ/LITELLM_UPSTREAM_BASE_URL
      api_key: os.environ/LITELLM_UPSTREAM_API_KEY
"""
    )

    assert parsed["model_list"][0]["litellm_params"]["api_key"] == (
        "os.environ/LITELLM_UPSTREAM_API_KEY"
    )


def test_internal_litellm_config_accepts_token_cost_metadata():
    parsed = parse_internal_litellm_config(
        """
model_list:
  - model_name: claude-opus
    litellm_params:
      model: openai/gpt
    model_info:
      input_cost_per_token: 0.000005
      output_cost_per_token: 0.00003
"""
    )

    assert parsed["model_list"][0]["model_info"] == {
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.00003,
    }


def test_internal_litellm_config_rejects_literal_secret():
    with pytest.raises(ValueError, match="litellm_params.api_key.*os.environ/NAME"):
        parse_internal_litellm_config(
            """
model_list:
  - model_name: claude-opus
    litellm_params:
      model: openai/gpt
      api_key: sk-literal-secret
"""
        )


@pytest.mark.parametrize("field", ["access_token", "langfuse_secret_key"])
def test_internal_litellm_config_rejects_literal_secret_suffixes(field):
    with pytest.raises(ValueError, match=field):
        parse_internal_litellm_config(
            f"""
model_list:
  - model_name: claude-opus
    litellm_params:
      {field}: literal-secret
"""
        )


def test_internal_litellm_config_rejects_literal_authorization_header():
    with pytest.raises(ValueError, match="extra_headers.Authorization"):
        parse_internal_litellm_config(
            """
model_list:
  - model_name: claude-opus
    litellm_params:
      extra_headers:
        Authorization: Bearer literal-secret
"""
        )


def test_internal_litellm_config_rejects_nested_environment_reference():
    with pytest.raises(ValueError, match="must be scalar model_list"):
        parse_internal_litellm_config(
            """
model_list:
  - model_name: claude-opus
    litellm_params:
      extra_headers:
        Authorization: os.environ/LITELLM_AUTHORIZATION
"""
        )


def test_internal_litellm_config_rejects_credentials_in_url():
    with pytest.raises(ValueError, match="must not contain credentials"):
        parse_internal_litellm_config(
            """
model_list:
  - model_name: claude-opus
    litellm_params:
      api_base: https://user:password@example.test/v1
"""
        )


def test_internal_litellm_config_rejects_credentials_in_url_query():
    with pytest.raises(ValueError, match="must not contain credentials"):
        parse_internal_litellm_config(
            """
model_list:
  - model_name: claude-opus
    litellm_params:
      api_base: https://example.test/v1?api-key=literal-secret
"""
        )


def test_internal_litellm_config_normalization_removes_comments():
    normalized = normalize_internal_litellm_config(
        """
# Local operator note that must not enter transport payloads.
model_list:
  - model_name: claude-opus
    litellm_params:
      api_key: os.environ/LITELLM_UPSTREAM_API_KEY
"""
    )

    assert "operator note" not in normalized
    assert "os.environ/LITELLM_UPSTREAM_API_KEY" in normalized


def test_internal_litellm_config_rejects_non_mapping_yaml():
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_internal_litellm_config("- model-name\n")


def test_internal_litellm_snapshot_rejects_missing_routing_file(tmp_path):
    from crsbench.utils.litellm_config import read_internal_litellm_config_snapshot

    experiment_path = tmp_path / "experiment.yaml"
    experiment_path.write_text(
        """
runtime:
  litellm:
    mode: internal
crs_compose:
  litellm_config_path: missing.yaml
"""
    )

    with pytest.raises(FileNotFoundError, match="Internal LiteLLM config file"):
        read_internal_litellm_config_snapshot(experiment_path)


def test_internal_litellm_path_treats_string_false_as_enabled():
    from crsbench.utils.litellm_config import internal_litellm_config_path

    path = internal_litellm_config_path(
        {
            "runtime": {
                "litellm": {"mode": "internal", "skip": "false"},
            },
            "crs_compose": {"litellm_config_path": "litellm.yaml"},
        }
    )

    assert path == "litellm.yaml"
