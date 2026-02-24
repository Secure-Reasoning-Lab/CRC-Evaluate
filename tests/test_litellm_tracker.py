"""Unit tests for LiteLLM usage tracking module."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests
from crsbench.evaluation.litellm_tracker import (
    DetailedLLMUsage,
    LiteLLMTracker,
    LiteLLMTrackerError,
    LLMTrackingContext,
    LLMUsageData,
    ModelUsageStats,
    is_tracking_available,
)


class TestLiteLLMTracker:
    """Tests for LiteLLMTracker class."""

    @pytest.fixture
    def tracker(self):
        """Create tracker with mock environment variables."""
        with patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_BASE_URL": "http://litellm:4000",
                "CRSBENCH_LLM_MASTER_KEY": "sk-master-key-123",
            },
            clear=True,
        ):
            return LiteLLMTracker()

    def test_init_with_env_vars(self):
        """Test initialization with environment variables."""
        with patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_BASE_URL": "http://litellm:4000",
                "CRSBENCH_LLM_MASTER_KEY": "sk-master-key-123",
            },
            clear=True,
        ):
            tracker = LiteLLMTracker()
            assert tracker.base_url == "http://litellm:4000"
            assert tracker.master_key == "sk-master-key-123"

    def test_init_with_explicit_params(self):
        """Test initialization with explicit parameters."""
        tracker = LiteLLMTracker(
            base_url="http://custom:8000",
            master_key="sk-custom-key",
        )
        assert tracker.base_url == "http://custom:8000"
        assert tracker.master_key == "sk-custom-key"

    def test_init_missing_base_url(self):
        """Test error when no runtime base URL is set."""
        with patch.dict(os.environ, {"CRSBENCH_LLM_MASTER_KEY": "sk-key"}, clear=True):
            with pytest.raises(
                LiteLLMTrackerError,
                match="CRSBENCH_LLM_BASE_URL/CRSBENCH_LLM_UPSTREAM_BASE_URL not set",
            ):
                LiteLLMTracker()

    def test_init_missing_master_key(self):
        """Test error when master key is not set."""
        with patch.dict(
            os.environ, {"CRSBENCH_LLM_BASE_URL": "http://litellm:4000"}, clear=True
        ):
            with pytest.raises(
                LiteLLMTrackerError,
                match="CRSBENCH_LLM_MASTER_KEY not set",
            ):
                LiteLLMTracker()

    def test_base_url_trailing_slash_removed(self):
        """Test that trailing slash is removed from base URL."""
        tracker = LiteLLMTracker(
            base_url="http://litellm:4000/",
            master_key="sk-key",
        )
        assert tracker.base_url == "http://litellm:4000"

    def test_build_key_alias(self, tracker):
        """Test key alias generation with random suffix."""
        alias = tracker._build_key_alias(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )
        # Should start with expected prefix and end with 8-char random suffix
        assert alias.startswith(
            "crsbench-exp1-atlantis-curl-fuzz_http-delta-address-trial1-"
        )
        # Random suffix is 8 hex characters
        suffix = alias.split("-")[-1]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_build_key_alias_unique(self, tracker):
        """Test key alias generates unique values each time."""
        alias1 = tracker._build_key_alias(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )
        alias2 = tracker._build_key_alias(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )
        # Same parameters should generate different aliases due to random suffix
        assert alias1 != alias2

    def test_build_key_alias_sanitizes_special_chars(self, tracker):
        """Test key alias sanitizes special characters."""
        alias = tracker._build_key_alias(
            experiment="exp/1",
            crs="atlantis:v2",
            benchmark="curl test",
            harness="fuzz http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )
        assert "/" not in alias
        assert ":" not in alias
        assert " " not in alias

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_generate_key_success(self, mock_post, tracker):
        """Test successful key generation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "key": "sk-generated-key-abc123",
            "key_alias": "crsbench-exp1-atlantis-curl-fuzz_http-trial1",
        }
        mock_post.return_value = mock_response

        key = tracker.generate_key(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        assert key == "sk-generated-key-abc123"
        mock_post.assert_called_once()

        # Verify request payload
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "http://litellm:4000/key/generate"
        payload = call_kwargs[1]["json"]
        # Key alias includes random suffix
        assert payload["key_alias"].startswith(
            "crsbench-exp1-atlantis-curl-fuzz_http-delta-address-trial1-"
        )
        assert payload["metadata"]["experiment"] == "exp1"
        assert payload["metadata"]["crs"] == "atlantis"

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_generate_key_with_budget(self, mock_post, tracker):
        """Test key generation with max budget."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "sk-key"}
        mock_post.return_value = mock_response

        tracker.generate_key(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=10.0,
        )

        payload = mock_post.call_args[1]["json"]
        assert payload["max_budget"] == 10.0

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_generate_key_with_team_id(self, mock_post, tracker):
        """Test key generation with team_id."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "sk-key"}
        mock_post.return_value = mock_response

        tracker.generate_key(
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            team_id="team-123",
        )

        payload = mock_post.call_args[1]["json"]
        assert payload["team_id"] == "team-123"

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_generate_key_api_error(self, mock_post, tracker):
        """Test key generation with API error."""
        mock_post.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to generate key"):
            tracker.generate_key(
                experiment="exp1",
                crs="atlantis",
                benchmark="curl",
                harness="fuzz_http",
                trial_num=1,
                mode="delta",
                sanitizer="address",
            )

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_generate_key_no_key_in_response(self, mock_post, tracker):
        """Test key generation when response has no key."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"error": "something went wrong"}
        mock_post.return_value = mock_response

        with pytest.raises(LiteLLMTrackerError, match="No key in response"):
            tracker.generate_key(
                experiment="exp1",
                crs="atlantis",
                benchmark="curl",
                harness="fuzz_http",
                trial_num=1,
                mode="delta",
                sanitizer="address",
            )

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_key_info_success(self, mock_get, tracker):
        """Test successful key info retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "info": {
                "key_alias": "crsbench-exp1-atlantis-curl-fuzz_http-trial1",
                "spend": 1.25,
                "max_budget": None,
                "metadata": {"experiment": "exp1"},
            }
        }
        mock_get.return_value = mock_response

        info = tracker.get_key_info("sk-test-key")

        assert info["info"]["spend"] == 1.25
        mock_get.assert_called_once()
        assert mock_get.call_args[1]["params"]["key"] == "sk-test-key"

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_key_info_api_error(self, mock_get, tracker):
        """Test key info with API error."""
        mock_get.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to get key info"):
            tracker.get_key_info("sk-test-key")

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_delete_key_success(self, mock_post, tracker):
        """Test successful key deletion."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "deleted_keys": ["sk-test-key"],
        }
        mock_post.return_value = mock_response

        result = tracker.delete_key("sk-test-key")

        assert result is True
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["json"]["keys"] == ["sk-test-key"]

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_delete_key_not_found(self, mock_post, tracker):
        """Test key deletion when key not in deleted list."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "deleted_keys": [],
        }
        mock_post.return_value = mock_response

        result = tracker.delete_key("sk-test-key")

        assert result is False

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_delete_key_api_error(self, mock_post, tracker):
        """Test key deletion with API error."""
        mock_post.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to delete key"):
            tracker.delete_key("sk-test-key")

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_find_team_by_alias_success(self, mock_get, tracker):
        """Test finding team by exact alias match."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"team_id": "team-123", "team_alias": "crsbench"},
            {"team_id": "team-456", "team_alias": "crsbench-test"},
        ]
        mock_get.return_value = mock_response

        team_id = tracker.find_team_by_alias("crsbench")

        assert team_id == "team-123"
        mock_get.assert_called_once()
        assert mock_get.call_args[1]["params"]["team_alias"] == "crsbench"

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_find_team_by_alias_no_match(self, mock_get, tracker):
        """Test finding team returns None when no exact match."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"team_id": "team-456", "team_alias": "crsbench-test"},
        ]
        mock_get.return_value = mock_response

        team_id = tracker.find_team_by_alias("crsbench")

        assert team_id is None

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_find_team_by_alias_dict_response(self, mock_get, tracker):
        """Test finding team with dict response format."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"team_id": "team-123", "team_alias": "crsbench"},
            ]
        }
        mock_get.return_value = mock_response

        team_id = tracker.find_team_by_alias("crsbench")

        assert team_id == "team-123"

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_find_team_by_alias_api_error(self, mock_get, tracker):
        """Test finding team handles API errors gracefully."""
        mock_get.side_effect = requests.RequestException("Connection failed")

        team_id = tracker.find_team_by_alias("crsbench")

        assert team_id is None

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_create_team_success(self, mock_post, tracker):
        """Test successful team creation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "team_id": "team-123",
            "team_alias": "crsbench",
        }
        mock_post.return_value = mock_response

        team_id = tracker.create_team("crsbench")

        assert team_id == "team-123"
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["team_alias"] == "crsbench"

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_create_team_no_team_id_in_response(self, mock_post, tracker):
        """Test team creation when response has no team_id."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"error": "something went wrong"}
        mock_post.return_value = mock_response

        with pytest.raises(LiteLLMTrackerError, match="No team_id in response"):
            tracker.create_team("crsbench")

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_create_team_api_error(self, mock_post, tracker):
        """Test team creation with API error."""
        mock_post.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to create team"):
            tracker.create_team("crsbench")

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_create_team_with_max_budget(self, mock_post, tracker):
        """Test team creation with max_budget parameter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "team_id": "team-123",
            "team_alias": "crsbench",
        }
        mock_post.return_value = mock_response

        team_id = tracker.create_team("crsbench", max_budget=100.0)

        assert team_id == "team-123"
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["team_alias"] == "crsbench"
        assert payload["max_budget"] == 100.0

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_update_team_success(self, mock_post, tracker):
        """Test successful team update."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success"}
        mock_post.return_value = mock_response

        tracker.update_team("team-123", max_budget=150.0)

        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "http://litellm:4000/team/update"
        payload = mock_post.call_args[1]["json"]
        assert payload["team_id"] == "team-123"
        assert payload["max_budget"] == 150.0

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    def test_update_team_api_error(self, mock_post, tracker):
        """Test team update with API error."""
        mock_post.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to update team"):
            tracker.update_team("team-123", max_budget=150.0)

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_team_info_success(self, mock_get, tracker):
        """Test successful team info retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "team_id": "team-123",
            "team_alias": "test-team",
            "spend": 42.50,
            "max_budget": 100.0,
        }
        mock_get.return_value = mock_response

        team_info = tracker.get_team_info("team-123")

        assert team_info["team_id"] == "team-123"
        assert team_info["spend"] == 42.50
        assert team_info["max_budget"] == 100.0
        mock_get.assert_called_once_with(
            "http://litellm:4000/team/info",
            headers=tracker._headers,
            params={"team_id": "team-123"},
            timeout=30,
        )

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_team_info_api_error(self, mock_get, tracker):
        """Test team info retrieval with API error."""
        mock_get.side_effect = requests.RequestException("Connection failed")

        with pytest.raises(LiteLLMTrackerError, match="Failed to get team info"):
            tracker.get_team_info("team-123")

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_or_create_team_uses_existing(self, mock_get, tracker):
        """Test get_or_create_team uses existing team when found."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"team_id": "team-123", "team_alias": "crsbench"},
        ]
        mock_get.return_value = mock_response

        team_id = tracker.get_or_create_team("crsbench")

        assert team_id == "team-123"
        # Should only call GET (find), not POST (create)
        mock_get.assert_called_once()

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_or_create_team_creates_new(self, mock_get, mock_post, tracker):
        """Test get_or_create_team creates team when not found."""
        # Mock find (returns None)
        mock_find_response = MagicMock()
        mock_find_response.status_code = 200
        mock_find_response.json.return_value = []
        mock_get.return_value = mock_find_response

        # Mock create
        mock_create_response = MagicMock()
        mock_create_response.status_code = 200
        mock_create_response.json.return_value = {
            "team_id": "team-456",
            "team_alias": "crsbench",
        }
        mock_post.return_value = mock_create_response

        team_id = tracker.get_or_create_team("crsbench")

        assert team_id == "team-456"
        # Should call both GET (find) and POST (create)
        mock_get.assert_called_once()
        mock_post.assert_called_once()

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_or_create_team_updates_existing_budget(
        self, mock_get, mock_post, tracker
    ):
        """Test get_or_create_team updates budget on existing team."""
        # Mock find (returns existing team)
        mock_find_response = MagicMock()
        mock_find_response.status_code = 200
        mock_find_response.json.return_value = [
            {"team_id": "team-123", "team_alias": "crsbench"},
        ]
        mock_get.return_value = mock_find_response

        # Mock update
        mock_update_response = MagicMock()
        mock_update_response.status_code = 200
        mock_update_response.json.return_value = {"status": "success"}
        mock_post.return_value = mock_update_response

        team_id = tracker.get_or_create_team("crsbench", max_budget=200.0)

        assert team_id == "team-123"
        # Should call GET (find) and POST (update)
        mock_get.assert_called_once()
        mock_post.assert_called_once()
        # Verify update was called with correct params
        assert mock_post.call_args[0][0] == "http://litellm:4000/team/update"
        payload = mock_post.call_args[1]["json"]
        assert payload["team_id"] == "team-123"
        assert payload["max_budget"] == 200.0

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_or_create_team_creates_with_budget(self, mock_get, mock_post, tracker):
        """Test get_or_create_team creates team with budget."""
        # Mock find (returns None)
        mock_find_response = MagicMock()
        mock_find_response.status_code = 200
        mock_find_response.json.return_value = []
        mock_get.return_value = mock_find_response

        # Mock create
        mock_create_response = MagicMock()
        mock_create_response.status_code = 200
        mock_create_response.json.return_value = {
            "team_id": "team-456",
            "team_alias": "crsbench",
        }
        mock_post.return_value = mock_create_response

        team_id = tracker.get_or_create_team("crsbench", max_budget=250.0)

        assert team_id == "team-456"
        # Should call GET (find) and POST (create)
        mock_get.assert_called_once()
        mock_post.assert_called_once()
        # Verify create was called with budget
        assert mock_post.call_args[0][0] == "http://litellm:4000/team/new"
        payload = mock_post.call_args[1]["json"]
        assert payload["team_alias"] == "crsbench"
        assert payload["max_budget"] == 250.0

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_generate_llm_usage_json(self, mock_get, tracker):
        """Test LLM usage data generation without detailed logs."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "info": {
                "key_alias": "crsbench-exp1-atlantis-curl-fuzz_http-trial1",
                "spend": 2.50,
                "max_budget": 10.0,
                "metadata": {"experiment": "exp1"},
            }
        }
        mock_get.return_value = mock_response

        usage = tracker.generate_llm_usage_json(
            api_key="sk-test-key",
            trial_id="exp1-atlantis-curl-fuzz_http-trial1",
            include_detailed_logs=False,
        )

        assert isinstance(usage, LLMUsageData)
        assert usage.trial_id == "exp1-atlantis-curl-fuzz_http-trial1"
        assert usage.total_spend_usd == 2.50
        assert usage.key_alias == "crsbench-exp1-atlantis-curl-fuzz_http-trial1"

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_spend_logs_success(self, mock_get, tracker):
        """Test successful spend logs retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "request_id": "req-1",
                "model": "gpt-4",
                "spend": 0.10,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cache_hit": False,
            },
            {
                "request_id": "req-2",
                "model": "gpt-4",
                "spend": 0.15,
                "prompt_tokens": 200,
                "completion_tokens": 75,
                "total_tokens": 275,
                "cache_hit": True,
            },
        ]
        mock_get.return_value = mock_response

        logs = tracker.get_spend_logs("sk-test-key")

        assert len(logs) == 2
        assert logs[0]["request_id"] == "req-1"
        assert logs[1]["cache_hit"] is True

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_spend_logs_dict_response(self, mock_get, tracker):
        """Test spend logs with dict response format."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"request_id": "req-1", "model": "gpt-4", "spend": 0.10},
            ]
        }
        mock_get.return_value = mock_response

        logs = tracker.get_spend_logs("sk-test-key")

        assert len(logs) == 1
        assert logs[0]["request_id"] == "req-1"

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_get_spend_logs_api_error_returns_empty(self, mock_get, tracker):
        """Test spend logs returns empty list on API error."""
        mock_get.side_effect = requests.RequestException("Connection failed")

        logs = tracker.get_spend_logs("sk-test-key")

        assert logs == []

    def test_aggregate_spend_logs(self, tracker):
        """Test aggregation of spend logs."""
        logs = [
            {
                "request_id": "req-1",
                "model": "gpt-4",
                "spend": 0.10,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cache_hit": False,
            },
            {
                "request_id": "req-2",
                "model": "gpt-4",
                "spend": 0.15,
                "prompt_tokens": 200,
                "completion_tokens": 75,
                "total_tokens": 275,
                "cache_hit": True,
            },
            {
                "request_id": "req-3",
                "model": "claude-3-opus",
                "spend": 0.25,
                "prompt_tokens": 300,
                "completion_tokens": 100,
                "total_tokens": 400,
                "cache_hit": False,
            },
        ]

        usage = tracker.aggregate_spend_logs(logs)

        assert usage.total_api_calls == 3
        assert usage.total_input_tokens == 600
        assert usage.total_output_tokens == 225
        assert usage.total_tokens == 825
        assert usage.total_cost_usd == 0.50
        assert usage.total_cache_hits == 1
        assert usage.total_cache_misses == 2

        # Check per-model stats
        assert "gpt-4" in usage.by_model
        assert "claude-3-opus" in usage.by_model
        assert usage.by_model["gpt-4"].calls == 2
        assert usage.by_model["claude-3-opus"].calls == 1

    def test_aggregate_spend_logs_empty(self, tracker):
        """Test aggregation with empty logs."""
        usage = tracker.aggregate_spend_logs([])

        assert usage.total_api_calls == 0
        assert usage.total_tokens == 0
        assert usage.total_cost_usd == 0.0
        assert len(usage.by_model) == 0

    def test_aggregate_spend_logs_handles_missing_fields(self, tracker):
        """Test aggregation handles missing fields gracefully."""
        logs = [
            {"model": "gpt-4"},  # Missing most fields
            {"model": "gpt-4", "spend": None, "prompt_tokens": None},  # None values
        ]

        usage = tracker.aggregate_spend_logs(logs)

        assert usage.total_api_calls == 2
        assert usage.total_tokens == 0
        assert usage.total_cost_usd == 0.0

    def test_aggregate_spend_logs_string_cache_hit(self, tracker):
        """Test aggregation handles string cache_hit values (LiteLLM API format)."""
        logs = [
            {"model": "gpt-4", "cache_hit": "True"},  # String "True"
            {"model": "gpt-4", "cache_hit": "False"},  # String "False"
            {"model": "gpt-4", "cache_hit": True},  # Boolean True
            {"model": "gpt-4", "cache_hit": False},  # Boolean False
        ]

        usage = tracker.aggregate_spend_logs(logs)

        assert usage.total_api_calls == 4
        assert usage.total_cache_hits == 2
        assert usage.total_cache_misses == 2

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_generate_llm_usage_json_with_detailed_logs(self, mock_get, tracker):
        """Test LLM usage generation with detailed logs."""
        # First call is for key/info, second is for spend/logs
        mock_key_info_response = MagicMock()
        mock_key_info_response.status_code = 200
        mock_key_info_response.json.return_value = {
            "info": {
                "key_alias": "test-alias",
                "spend": 1.00,
                "max_budget": None,
                "metadata": {},
            }
        }

        mock_spend_logs_response = MagicMock()
        mock_spend_logs_response.status_code = 200
        mock_spend_logs_response.json.return_value = [
            {
                "request_id": "req-1",
                "model": "gpt-4",
                "spend": 0.50,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cache_hit": False,
            },
            {
                "request_id": "req-2",
                "model": "gpt-4",
                "spend": 0.50,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cache_hit": False,
            },
        ]

        mock_get.side_effect = [mock_key_info_response, mock_spend_logs_response]

        usage = tracker.generate_llm_usage_json(
            api_key="sk-test-key",
            trial_id="test-trial",
            include_detailed_logs=True,
        )

        assert usage.detailed_usage is not None
        assert usage.detailed_usage.total_api_calls == 2
        assert usage.detailed_usage.total_tokens == 300
        # Total spend should be updated from detailed logs
        assert usage.total_spend_usd == 1.00

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_write_llm_usage_file(self, mock_get, tracker, tmp_path):
        """Test writing LLM usage to file."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "info": {
                "key_alias": "test-alias",
                "spend": 1.00,
                "max_budget": None,
                "metadata": {},
            }
        }
        mock_get.return_value = mock_response

        output_path = tmp_path / "llm-usage.json"
        result_path = tracker.write_llm_usage_file(
            api_key="sk-test-key",
            trial_id="test-trial",
            output_path=output_path,
        )

        assert result_path == output_path
        assert output_path.exists()

        data = json.loads(output_path.read_text())
        assert data["trial_id"] == "test-trial"
        assert data["total_cost_usd"] == 1.00

    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_write_llm_logs_file(self, mock_get, tracker, tmp_path):
        """Test writing detailed LLM logs to file."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "request_id": "req-1",
                "model": "gpt-4",
                "spend": 0.50,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "messages": [{"role": "user", "content": "Hello"}],
                "response": "Hi there!",
            },
            {
                "request_id": "req-2",
                "model": "gpt-4",
                "spend": 0.30,
                "prompt_tokens": 80,
                "completion_tokens": 40,
                "messages": [{"role": "user", "content": "Test"}],
                "response": "Response",
            },
        ]
        mock_get.return_value = mock_response

        output_path = tmp_path / "llm-logs.json"
        result_path = tracker.write_llm_logs_file(
            api_key="sk-test-key",
            trial_id="test-trial",
            output_path=output_path,
        )

        assert result_path == output_path
        assert output_path.exists()

        data = json.loads(output_path.read_text())
        assert data["trial_id"] == "test-trial"
        assert data["total_requests"] == 2
        assert len(data["logs"]) == 2
        # Verify raw logs are preserved
        assert data["logs"][0]["messages"] == [{"role": "user", "content": "Hello"}]
        assert data["logs"][0]["response"] == "Hi there!"


class TestModelUsageStats:
    """Tests for ModelUsageStats dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        stats = ModelUsageStats(
            model="gpt-4",
            calls=10,
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_usd=0.50,
            cache_hits=3,
            cache_misses=7,
        )

        data = stats.to_dict()

        assert data["model"] == "gpt-4"
        assert data["calls"] == 10
        assert data["input_tokens"] == 1000
        assert data["output_tokens"] == 500
        assert data["total_tokens"] == 1500
        assert data["cost_usd"] == 0.50
        assert data["cache_hits"] == 3
        assert data["cache_misses"] == 7


class TestDetailedLLMUsage:
    """Tests for DetailedLLMUsage dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        usage = DetailedLLMUsage(
            total_api_calls=20,
            total_input_tokens=2000,
            total_output_tokens=1000,
            total_tokens=3000,
            total_cost_usd=1.50,
            total_cache_hits=5,
            total_cache_misses=15,
            by_model={
                "gpt-4": ModelUsageStats(
                    model="gpt-4",
                    calls=10,
                    input_tokens=1000,
                    output_tokens=500,
                    total_tokens=1500,
                    cost_usd=0.75,
                ),
            },
            request_logs=[{"request_id": "req-1"}],
        )

        data = usage.to_dict()

        assert data["total_api_calls"] == 20
        assert data["total_input_tokens"] == 2000
        assert data["total_output_tokens"] == 1000
        assert data["total_tokens"] == 3000
        assert data["total_cost_usd"] == 1.50
        assert data["total_cache_hits"] == 5
        assert data["total_cache_misses"] == 15
        assert "gpt-4" in data["by_model"]
        assert data["request_count"] == 1

    def test_default_values(self):
        """Test default values are properly initialized."""
        usage = DetailedLLMUsage()

        assert usage.total_api_calls == 0
        assert usage.total_tokens == 0
        assert usage.total_cost_usd == 0.0
        assert len(usage.by_model) == 0
        assert len(usage.request_logs) == 0


class TestLLMUsageData:
    """Tests for LLMUsageData dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        usage = LLMUsageData(
            trial_id="test-trial",
            timestamp="2025-01-09T12:00:00Z",
            total_spend_usd=1.50,
            key_alias="test-alias",
            key_info={"key_alias": "test-alias", "spend": 1.50},
            raw_response={"info": {"spend": 1.50}},
        )

        data = usage.to_dict()

        assert data["trial_id"] == "test-trial"
        assert data["timestamp"] == "2025-01-09T12:00:00Z"
        assert data["total_cost_usd"] == 1.50
        assert data["key_alias"] == "test-alias"

    def test_to_dict_with_detailed_usage(self):
        """Test conversion includes detailed usage when present."""
        detailed = DetailedLLMUsage(
            total_api_calls=5,
            total_tokens=500,
            total_cost_usd=0.25,
        )

        usage = LLMUsageData(
            trial_id="test-trial",
            timestamp="2025-01-09T12:00:00Z",
            total_spend_usd=0.25,
            key_alias="test-alias",
            key_info={},
            raw_response={},
            detailed_usage=detailed,
        )

        data = usage.to_dict()

        assert data["total_api_calls"] == 5
        assert data["total_tokens"] == 500
        assert "by_model" in data

    def test_to_dict_without_detailed_usage(self):
        """Test conversion provides defaults when no detailed usage."""
        usage = LLMUsageData(
            trial_id="test-trial",
            timestamp="2025-01-09T12:00:00Z",
            total_spend_usd=0.25,
            key_alias="test-alias",
            key_info={},
            raw_response={},
            detailed_usage=None,
        )

        data = usage.to_dict()

        # Should have default values
        assert data["total_api_calls"] == 0
        assert data["total_tokens"] == 0
        assert data["by_model"] == {}


class TestLLMTrackingContext:
    """Tests for LLMTrackingContext context manager."""

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_context_manager_lifecycle(self, mock_get, mock_post, tmp_path):
        """Test full context manager lifecycle."""
        # Mock key generation
        mock_generate_response = MagicMock()
        mock_generate_response.status_code = 200
        mock_generate_response.json.return_value = {"key": "sk-generated-key"}

        # Mock key deletion
        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 200
        mock_delete_response.json.return_value = {"deleted_keys": ["sk-generated-key"]}

        mock_post.side_effect = [mock_generate_response, mock_delete_response]

        # Mock key info
        mock_info_response = MagicMock()
        mock_info_response.status_code = 200
        mock_info_response.json.return_value = {
            "info": {
                "key_alias": "test-alias",
                "spend": 0.50,
                "max_budget": None,
                "metadata": {},
            }
        }
        mock_get.return_value = mock_info_response

        tracker = LiteLLMTracker(
            base_url="http://litellm:4000",
            master_key="sk-master",
        )

        with LLMTrackingContext(
            tracker=tracker,
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            output_dir=tmp_path,
        ) as ctx:
            assert ctx.api_key == "sk-generated-key"
            assert ctx.trial_id is not None

        # Verify file was written
        usage_file = tmp_path / "llm-usage.json"
        assert usage_file.exists()

        # Verify key was deleted (second POST call)
        assert mock_post.call_count == 2

    @patch("crsbench.evaluation.litellm_tracker.requests.post")
    @patch("crsbench.evaluation.litellm_tracker.requests.get")
    def test_context_manager_writes_intermediate(self, mock_get, mock_post, tmp_path):
        """Test intermediate usage file write."""
        # Mock key generation
        mock_generate_response = MagicMock()
        mock_generate_response.status_code = 200
        mock_generate_response.json.return_value = {"key": "sk-key"}

        # Mock key deletion
        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 200
        mock_delete_response.json.return_value = {"deleted_keys": ["sk-key"]}

        mock_post.side_effect = [mock_generate_response, mock_delete_response]

        # Mock key info
        mock_info_response = MagicMock()
        mock_info_response.status_code = 200
        mock_info_response.json.return_value = {
            "info": {
                "key_alias": "alias",
                "spend": 0.25,
                "max_budget": None,
                "metadata": {},
            }
        }
        mock_get.return_value = mock_info_response

        tracker = LiteLLMTracker(base_url="http://litellm:4000", master_key="sk-master")

        with LLMTrackingContext(
            tracker=tracker,
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            output_dir=tmp_path,
        ) as ctx:
            # Write intermediate usage
            path = ctx.write_intermediate_usage()
            assert path is not None
            assert path.exists()


class TestIsTrackingAvailable:
    """Tests for is_tracking_available function."""

    def test_available_when_both_env_vars_set(self):
        """Test returns True when both env vars are set."""
        with patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_BASE_URL": "http://litellm:4000",
                "CRSBENCH_LLM_MASTER_KEY": "sk-key",
            },
            clear=True,
        ):
            assert is_tracking_available() is True

    def test_available_with_upstream_base_url(self):
        """Test returns True when CRSBENCH_LLM_UPSTREAM_BASE_URL is used."""
        with patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "http://upstream:4000",
                "CRSBENCH_LLM_MASTER_KEY": "sk-key",
            },
            clear=True,
        ):
            assert is_tracking_available() is True

    def test_unavailable_when_base_url_missing(self):
        """Test returns False when base URL is missing."""
        with patch.dict(os.environ, {"CRSBENCH_LLM_MASTER_KEY": "sk-key"}, clear=True):
            assert is_tracking_available() is False

    def test_unavailable_when_master_key_missing(self):
        """Test returns False when master key is missing."""
        with patch.dict(
            os.environ, {"CRSBENCH_LLM_BASE_URL": "http://litellm:4000"}, clear=True
        ):
            assert is_tracking_available() is False

    def test_unavailable_when_both_missing(self):
        """Test returns False when both env vars are missing."""
        with patch.dict(os.environ, {}, clear=True):
            assert is_tracking_available() is False


class TestSetupLlmTrackingBudget:
    """Tests for _setup_llm_tracking budget propagation."""

    @pytest.fixture(autouse=True)
    def litellm_env(self):
        """Provide LiteLLM env vars expected by tracking setup."""
        with patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_BASE_URL": "http://litellm:4000",
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "http://litellm:4000",
                "CRSBENCH_LLM_MASTER_KEY": "sk-master-key-123",
                "CRSBENCH_LLM_UPSTREAM_API_KEY": "sk-api-key-123",
            },
            clear=False,
        ):
            yield

    @pytest.fixture
    def base_config_dict(self, tmp_path):
        """Base config dict with required fields."""
        return {
            "experiment": "test-exp",
            "trials": 1,
            "mode": "delta",
            "adapter": "oss-crs",
            "max_total_time": 36000,  # Must be > build + run + verify timeouts
            "build_timeout": 3600,
            "run_timeout": 7200,
            "verify_timeout": 7200,
            "difficulty_level": 0,
            "experiment_filestore": tmp_path / "experiments",
            "report_filestore": tmp_path / "reports",
            "crses": ["test-crs"],
            "benchmarks": ["test-bench"],
        }

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_budget_passed_to_generate_key(self, mock_tracker_class, base_config_dict):
        """Test that cost_budget from config is passed to generate_key()."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import (
            ExperimentConfig,
            LitellmResourceConfig,
            ResourceConfig,
        )

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker.get_or_create_team.return_value = "team-123"
        mock_tracker.get_team_info.return_value = {"spend": 10.0, "max_budget": 100.0}
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(
            **base_config_dict,
            resources=ResourceConfig(litellm=LitellmResourceConfig(cost_budget=50.0)),
        )

        tracker, api_key = _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        assert api_key == "sk-test-key"
        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=50.0,
        )

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_no_budget_when_resources_not_configured(
        self, mock_tracker_class, base_config_dict
    ):
        """Test that max_budget is None when resources.litellm not configured."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import ExperimentConfig

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker.get_or_create_team.return_value = "team-123"
        mock_tracker.get_team_info.return_value = {"spend": 10.0, "max_budget": None}
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(**base_config_dict)

        _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=None,
        )

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_no_budget_when_litellm_not_configured(
        self, mock_tracker_class, base_config_dict
    ):
        """Test that max_budget is None when litellm not in resources."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import ExperimentConfig, ResourceConfig

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker.get_or_create_team.return_value = "team-123"
        mock_tracker.get_team_info.return_value = {"spend": 10.0, "max_budget": None}
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(
            **base_config_dict,
            resources=ResourceConfig(),  # No litellm configured
        )

        _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        # Team association disabled (LiteLLM bug #11962)
        mock_tracker.get_or_create_team.assert_not_called()

        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=None,
        )

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_team_from_config(self, mock_tracker_class, base_config_dict):
        """Test that team config is accepted but team association is disabled."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import (
            ExperimentConfig,
            LitellmResourceConfig,
            ResourceConfig,
        )

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(
            **base_config_dict,
            resources=ResourceConfig(
                litellm=LitellmResourceConfig(cost_budget=50.0, team="custom-team")
            ),
        )

        _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        # Team association disabled (LiteLLM bug #11962)
        mock_tracker.get_or_create_team.assert_not_called()

        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=50.0,
        )

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_team_defaults_to_experiment_name(
        self, mock_tracker_class, base_config_dict
    ):
        """Test that keys are generated without team association."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import ExperimentConfig

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(**base_config_dict)

        _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        # Team association disabled (LiteLLM bug #11962)
        mock_tracker.get_or_create_team.assert_not_called()

        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=None,
        )

    @patch("crsbench.distributed.jobs.LiteLLMTracker")
    def test_team_config_ignored_key_generated_independently(
        self, mock_tracker_class, base_config_dict
    ):
        """Test that team/team_max_budget config is ignored; key is independent."""
        from crsbench.distributed.jobs import _setup_llm_tracking
        from crsbench.validation.schemas import (
            ExperimentConfig,
            LitellmResourceConfig,
            ResourceConfig,
        )

        mock_tracker = MagicMock()
        mock_tracker.generate_key.return_value = "sk-test-key"
        mock_tracker_class.return_value = mock_tracker

        config = ExperimentConfig(
            **base_config_dict,
            resources=ResourceConfig(
                litellm=LitellmResourceConfig(
                    cost_budget=50.0,
                    team="custom-team",
                    team_max_budget=500.0,
                )
            ),
        )

        tracker, api_key = _setup_llm_tracking(
            config=config,
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
        )

        assert api_key == "sk-test-key"
        # Team association disabled (LiteLLM bug #11962)
        mock_tracker.get_or_create_team.assert_not_called()

        # Key generated independently without team_id
        mock_tracker.generate_key.assert_called_once_with(
            experiment="test-exp",
            crs="test-crs",
            benchmark="test-bench",
            harness="fuzz_test",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            max_budget=50.0,
        )
