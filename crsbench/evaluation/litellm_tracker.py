"""LiteLLM usage tracking via Virtual Keys API.

This module provides functionality to track LLM usage per trial using
LiteLLM's Virtual Keys feature. Each trial gets a unique API key, enabling
accurate per-trial cost and token tracking.

Reference:
    - LiteLLM Virtual Keys: https://docs.litellm.ai/docs/proxy/virtual_keys
    - LiteLLM Spend Tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from crsbench.utils.litellm_env import resolve_litellm_runtime_env
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Type aliases
type KeyAlias = str
type ApiKey = str


@dataclass
class ModelUsageStats:
    """Usage statistics for a specific model."""

    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


@dataclass
class DetailedLLMUsage:
    """Detailed LLM usage metrics aggregated from spend logs."""

    total_api_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_cache_hits: int = 0
    total_cache_misses: int = 0
    by_model: dict[str, ModelUsageStats] = field(default_factory=dict)
    request_logs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_api_calls": self.total_api_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "total_cache_hits": self.total_cache_hits,
            "total_cache_misses": self.total_cache_misses,
            "by_model": {k: v.to_dict() for k, v in self.by_model.items()},
            "request_count": len(self.request_logs),
        }


@dataclass
class LLMUsageData:
    """LLM usage data for a trial."""

    trial_id: str
    timestamp: str
    total_spend_usd: float
    key_alias: str
    key_info: dict
    raw_response: dict
    detailed_usage: Optional[DetailedLLMUsage] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "trial_id": self.trial_id,
            "timestamp": self.timestamp,
            "total_cost_usd": self.total_spend_usd,
            "key_alias": self.key_alias,
            "key_info": self.key_info,
        }

        # Add detailed usage if available
        if self.detailed_usage:
            detailed_usage = self.detailed_usage.to_dict()
            # Preserve key/accounting spend as the authoritative top-level value.
            detailed_usage.pop("total_cost_usd", None)
            result.update(detailed_usage)
        else:
            # Provide empty defaults for compatibility
            result.update(
                {
                    "total_api_calls": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_tokens": 0,
                    "total_cache_hits": 0,
                    "total_cache_misses": 0,
                    "by_model": {},
                    "request_count": 0,
                }
            )

        return result


class LiteLLMTrackerError(Exception):
    """Exception raised for LiteLLM tracking errors."""


class LiteLLMTracker:
    """Tracks LLM usage per trial via LiteLLM Virtual Keys API.

    This class manages the lifecycle of trial-specific API keys:
    1. Generate a unique key at trial start
    2. Query spend/usage during snapshots or at trial end
    3. Delete the key after trial completion

    Environment Variables:
        CRSBENCH_LLM_BASE_URL / CRSBENCH_LLM_UPSTREAM_BASE_URL:
            LiteLLM URL(s) used for runtime and/or forwarding.
        CRSBENCH_LLM_UPSTREAM_MASTER_KEY: Master key for key management APIs
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        master_key: Optional[str] = None,
        *,
        timeout: int = 60,
        max_retries: int = 3,
        max_backoff: float = 60.0,
    ):
        """Initialize LiteLLM tracker.

        Args:
            base_url: LiteLLM proxy URL. Defaults to resolved CRSBench runtime
                URL for current mode.
            master_key: Master key for API. Defaults to resolved CRSBench
                runtime key.
            timeout: Per-attempt request timeout in seconds.
            max_retries: Maximum number of retry attempts for transient failures.
            max_backoff: Maximum total backoff time in seconds across all retries.

        Raises:
            LiteLLMTrackerError: If required environment variables are not set.
        """
        runtime_env = resolve_litellm_runtime_env(
            os.environ.get("CRSBENCH_LLM_MODE", "external")
        )
        self.base_url = base_url or runtime_env.tracking_base_url
        self.master_key = master_key or runtime_env.master_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_backoff = max_backoff

        if not self.base_url:
            raise LiteLLMTrackerError(
                "CRSBENCH_LLM_BASE_URL/CRSBENCH_LLM_UPSTREAM_BASE_URL not set. "
                "Required for LLM tracking."
            )
        if not self.master_key:
            raise LiteLLMTrackerError(
                "CRSBENCH_LLM_UPSTREAM_MASTER_KEY not set. Required for LLM tracking."
            )

        # Remove trailing slash from base URL
        self.base_url = self.base_url.rstrip("/")

        self._headers = {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }
        self._key_info_cache: dict[ApiKey, tuple[float, dict]] = {}

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        operation: str = "request",
        **kwargs,
    ) -> requests.Response:
        """Execute an HTTP request with exponential backoff retry.

        Retries on connection errors, timeouts, and 5xx server errors.
        Total backoff time is capped at ``self.max_backoff`` seconds.

        Args:
            method: HTTP method ("get" or "post").
            url: Request URL.
            operation: Human-readable name for log messages.
            **kwargs: Forwarded to ``requests.request()``.

        Returns:
            Successful ``requests.Response``.

        Raises:
            requests.RequestException: After all retries are exhausted.
        """
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", self._headers)

        last_exc: Optional[Exception] = None
        total_wait = 0.0

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.request(method, url, **kwargs)
                if response.status_code < 500:
                    return response
                # 5xx — retry
                last_exc = requests.HTTPError(
                    f"Server error {response.status_code}", response=response
                )
            except (
                requests.ConnectionError,
                requests.Timeout,
            ) as exc:
                last_exc = exc

            if attempt == self.max_retries:
                break

            # Exponential backoff: 1s, 2s, 4s, 8s, ...
            delay = min(2 ** (attempt - 1), self.max_backoff - total_wait)
            if delay <= 0:
                break
            logger.debug(
                f"{operation} attempt {attempt}/{self.max_retries} failed, "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            total_wait += delay
            if total_wait >= self.max_backoff:
                break

        raise last_exc  # type: ignore[misc]

    def find_team_by_alias(self, team_alias: str) -> Optional[str]:
        """Find a team by exact alias match.

        Uses /v2/team/list with team_alias parameter for partial search,
        then filters for exact match.

        Args:
            team_alias: Team alias to search for

        Returns:
            team_id if exact match found, None otherwise
        """
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/v2/team/list",
                operation="find_team",
                params={"team_alias": team_alias},
            )
            response.raise_for_status()
            data = response.json()

            # Search results for exact alias match
            teams = (
                data
                if isinstance(data, list)
                else data.get("teams", data.get("data", []))
            )
            for team in teams:
                if team.get("team_alias") == team_alias:
                    return team.get("team_id")
            return None

        except requests.RequestException as e:
            logger.warning(f"Failed to search teams: {e}")
            return None

    def create_team(
        self, team_alias: str, *, max_budget: Optional[float] = None
    ) -> str:
        """Create a new team.

        Args:
            team_alias: Team alias/name
            max_budget: Optional maximum budget for the team in USD

        Returns:
            team_id of created team

        Raises:
            LiteLLMTrackerError: If team creation fails
        """
        payload: dict = {"team_alias": team_alias}
        if max_budget is not None:
            payload["max_budget"] = max_budget

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/team/new",
                operation="create_team",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            team_id = data.get("team_id")
            if not team_id:
                raise LiteLLMTrackerError(f"No team_id in response: {data}")

            budget_info = f" (max_budget: ${max_budget})" if max_budget else ""
            logger.info(f"Created LiteLLM team: {team_alias} ({team_id}){budget_info}")
            return team_id

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to create team: {e}") from e

    def update_team(self, team_id: str, *, max_budget: Optional[float] = None) -> None:
        """Update an existing team's settings.

        Args:
            team_id: Team ID to update
            max_budget: Optional new maximum budget for the team in USD

        Raises:
            LiteLLMTrackerError: If team update fails
        """
        payload: dict = {"team_id": team_id}
        if max_budget is not None:
            payload["max_budget"] = max_budget

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/team/update",
                operation="update_team",
                json=payload,
            )
            response.raise_for_status()
            logger.info(f"Updated LiteLLM team {team_id}: max_budget=${max_budget}")

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to update team: {e}") from e

    def get_or_create_team(
        self, team_alias: str, *, max_budget: Optional[float] = None
    ) -> str:
        """Get existing team or create new one, ensuring budget is set.

        1. Search for team by alias using /v2/team/list
        2. If exact match found:
           - Update team budget if max_budget provided
           - Return existing team_id
        3. If no match, create new team with max_budget

        Args:
            team_alias: Team alias/name
            max_budget: Optional maximum budget for the team in USD

        Returns:
            team_id (existing or newly created)

        Raises:
            LiteLLMTrackerError: If both search and creation fail
        """
        # First, search for existing team
        team_id = self.find_team_by_alias(team_alias)
        if team_id:
            logger.info(f"Using existing LiteLLM team: {team_alias} ({team_id})")
            # Update budget if provided
            if max_budget is not None:
                self.update_team(team_id, max_budget=max_budget)
            return team_id

        # No existing team, create new one
        return self.create_team(team_alias, max_budget=max_budget)

    def get_team_info(self, team_id: str) -> dict:
        """Get team information including spend and budget.

        Args:
            team_id: Team ID to query

        Returns:
            Team info dict with spend, max_budget, etc.

        Raises:
            LiteLLMTrackerError: If query fails
        """
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/team/info",
                operation="get_team_info",
                params={"team_id": team_id},
            )
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to get team info: {e}") from e

    def generate_key(
        self,
        experiment: str,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str,
        sanitizer: str,
        *,
        team_id: Optional[str] = None,
        max_budget: Optional[float] = None,
    ) -> ApiKey:
        """Generate a trial-specific API key.

        Args:
            experiment: Experiment name
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: Build mode
            sanitizer: Sanitizer type
            team_id: Optional team ID for key association
            max_budget: Optional maximum budget for the key

        Returns:
            Generated API key string

        Raises:
            LiteLLMTrackerError: If key generation fails
        """
        key_alias = self._build_key_alias(
            experiment, crs, benchmark, harness, trial_num, mode, sanitizer
        )

        payload: dict = {
            "key_alias": key_alias,
            "key_type": "llm_api",
            "models": [],  # Empty means all models allowed
            "metadata": {
                "experiment": experiment,
                "crs": crs,
                "benchmark": benchmark,
                "harness": harness,
                "trial_num": trial_num,
                "created_by": "crsbench",
            },
        }

        if team_id:
            payload["team_id"] = team_id
        if max_budget is not None:
            payload["max_budget"] = max_budget

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/key/generate",
                operation="generate_key",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            api_key = data.get("key")
            if not api_key:
                raise LiteLLMTrackerError(f"No key in response: {data}")

            logger.info(f"Generated LiteLLM key: {key_alias}")
            return api_key

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to generate key: {e}") from e

    def get_key_info(self, api_key: ApiKey) -> dict:
        """Get information about a specific key.

        Args:
            api_key: The API key to query

        Returns:
            Key information dictionary containing spend, metadata, etc.

        Raises:
            LiteLLMTrackerError: If query fails
        """
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/key/info",
                operation="get_key_info",
                params={"key": api_key},
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Retrieved key info for {api_key[:20]}...")
            return data

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to get key info: {e}") from e

    def _get_key_info_cached(
        self, api_key: ApiKey, *, max_age_seconds: float = 1.0
    ) -> dict:
        """Return cached key info when the previous read is still fresh."""
        cached = self._key_info_cache.get(api_key)
        now = time.monotonic()
        if cached is not None:
            cached_at, payload = cached
            if now - cached_at <= max_age_seconds:
                return payload

        payload = self.get_key_info(api_key)
        self._key_info_cache[api_key] = (now, payload)
        return payload

    def _build_key_usage_snapshot(self, key_info: dict) -> dict:
        """Build the persisted key-level accounting snapshot."""
        info = key_info.get("info", {}) if isinstance(key_info, dict) else {}
        key_alias = info.get("key_alias", "unknown")
        key_info_spend = float(info.get("spend", 0.0) or 0.0)
        return {
            "key_alias": key_alias,
            "spend": key_info_spend,
            "max_budget": info.get("max_budget"),
            "metadata": info.get("metadata", {}),
            "spend_sources": {
                "key_info_spend_usd": key_info_spend,
                "selected_total_spend_usd": key_info_spend,
                "selection_rule": "key/info",
                "spend_log_tracking_suppressed": True,
            },
        }

    def delete_key(self, api_key: ApiKey) -> bool:
        """Delete an API key.

        Args:
            api_key: The API key to delete

        Returns:
            True if deletion succeeded, False otherwise

        Raises:
            LiteLLMTrackerError: If deletion fails
        """
        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/key/delete",
                operation="delete_key",
                json={"keys": [api_key]},
            )
            response.raise_for_status()
            data = response.json()

            deleted_keys = data.get("deleted_keys", [])
            if api_key in deleted_keys:
                logger.info(f"Deleted LiteLLM key: {api_key[:20]}...")
                return True

            logger.warning(f"Key not in deleted list: {api_key[:20]}...")
            return False

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to delete key: {e}") from e

    def get_spend_logs(
        self,
        api_key: ApiKey,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Get spend logs for a specific API key.

        Retrieves detailed request logs from LiteLLM's spend tracking,
        including per-request token counts, costs, and model information.

        Args:
            api_key: The API key to query logs for
            start_date: Optional start date filter (ISO format)
            end_date: Optional end date filter (ISO format)

        Returns:
            List of spend log entries, each containing:
            - request_id: Unique request identifier
            - model: Model used (e.g., "gpt-4", "claude-3-opus")
            - call_type: Type of call ("completion", "embedding", etc.)
            - spend: Cost in USD for this request
            - total_tokens: Total tokens used
            - prompt_tokens: Input tokens
            - completion_tokens: Output tokens
            - startTime: Request start timestamp
            - endTime: Request end timestamp
            - cache_hit: Whether response was cached

        Raises:
            LiteLLMTrackerError: If query fails
        """
        try:
            params: dict = {"api_key": api_key}
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date

            response = self._request_with_retry(
                "get",
                f"{self.base_url}/spend/logs",
                operation="get_spend_logs",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            # LiteLLM returns logs in various formats depending on version
            # Handle both list and dict responses
            if isinstance(data, list):
                logs = data
            elif isinstance(data, dict):
                logs = data.get("data", data.get("logs", []))
            else:
                logs = []

            logger.debug(f"Retrieved {len(logs)} spend logs for key {api_key[:20]}...")
            return logs

        except requests.RequestException as e:
            logger.warning(f"Failed to get spend logs: {e}")
            # Return empty list instead of raising - spend logs are optional
            return []

    def aggregate_spend_logs(self, logs: list[dict]) -> DetailedLLMUsage:
        """Aggregate spend logs into detailed usage metrics.

        Args:
            logs: List of spend log entries from get_spend_logs()

        Returns:
            DetailedLLMUsage with aggregated metrics by model
        """
        usage = DetailedLLMUsage()
        model_stats: dict[str, ModelUsageStats] = {}

        for log in logs:
            # Extract fields with safe defaults
            model = log.get("model", "unknown")
            spend = float(log.get("spend", 0) or 0)
            prompt_tokens = int(log.get("prompt_tokens", 0) or 0)
            completion_tokens = int(log.get("completion_tokens", 0) or 0)
            total_tokens = int(
                log.get("total_tokens", prompt_tokens + completion_tokens) or 0
            )
            # cache_hit can be bool or string ("True"/"False") depending on LiteLLM version
            cache_hit_raw = log.get("cache_hit", False)
            if isinstance(cache_hit_raw, str):
                cache_hit = cache_hit_raw.lower() == "true"
            else:
                cache_hit = bool(cache_hit_raw)

            # Update totals
            usage.total_api_calls += 1
            usage.total_input_tokens += prompt_tokens
            usage.total_output_tokens += completion_tokens
            usage.total_tokens += total_tokens
            usage.total_cost_usd += spend

            if cache_hit:
                usage.total_cache_hits += 1
            else:
                usage.total_cache_misses += 1

            # Update per-model stats
            if model not in model_stats:
                model_stats[model] = ModelUsageStats(model=model)

            stats = model_stats[model]
            stats.calls += 1
            stats.input_tokens += prompt_tokens
            stats.output_tokens += completion_tokens
            stats.total_tokens += total_tokens
            stats.cost_usd += spend
            if cache_hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1

            # Store simplified log entry
            usage.request_logs.append(
                {
                    "request_id": log.get("request_id", ""),
                    "model": model,
                    "call_type": log.get("call_type", "completion"),
                    "spend": spend,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "cache_hit": cache_hit,
                    "startTime": log.get("startTime", ""),
                    "endTime": log.get("endTime", ""),
                }
            )

        usage.by_model = model_stats
        return usage

    def generate_llm_usage_json(
        self,
        api_key: ApiKey,
        trial_id: str,
        *,
        include_detailed_logs: bool = True,
    ) -> LLMUsageData:
        """Generate LLM usage data for a trial.

        Args:
            api_key: The trial's API key
            trial_id: Trial identifier for the output
            include_detailed_logs: Deprecated compatibility flag. CRSBench
                now derives trial usage from ``/key/info`` only.

        Returns:
            LLMUsageData with usage information including detailed metrics

        Raises:
            LiteLLMTrackerError: If usage query fails
        """
        key_info = self._get_key_info_cached(api_key)
        key_usage = self._build_key_usage_snapshot(key_info)
        spend = float(key_usage["spend"] or 0.0)
        key_alias = str(key_usage["key_alias"])

        if include_detailed_logs:
            logger.debug(
                "Ignoring include_detailed_logs=True for "
                f"{trial_id}; CRSBench uses LiteLLM /key/info only and "
                "suppresses /spend/logs scans."
            )

        return LLMUsageData(
            trial_id=trial_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_spend_usd=spend,
            key_alias=key_alias,
            key_info=key_usage,
            raw_response=key_info,
            detailed_usage=None,
        )

    def write_llm_usage_file(
        self,
        api_key: ApiKey,
        trial_id: str,
        output_path: Path,
    ) -> Path:
        """Query usage and write llm-usage.json file.

        Args:
            api_key: The trial's API key
            trial_id: Trial identifier
            output_path: Path to write the JSON file

        Returns:
            Path to the written file

        Raises:
            LiteLLMTrackerError: If usage query fails
        """
        usage_data = self.generate_llm_usage_json(api_key, trial_id)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(usage_data.to_dict(), indent=2))

        logger.info(
            f"Wrote LLM usage to {output_path}: ${usage_data.total_spend_usd:.4f}"
        )
        return output_path

    def write_llm_logs_file(
        self,
        api_key: ApiKey,
        trial_id: str,
        output_path: Path,
    ) -> Path:
        """Write detailed LLM conversation logs to a JSON file.

        This saves the raw spend logs with all available fields including
        messages and responses (if store_prompts_in_spend_logs is enabled
        on the LiteLLM server).

        Args:
            api_key: The trial's API key
            trial_id: Trial identifier
            output_path: Path to write the JSON file

        Returns:
            Path to the written file
        """
        key_info = self._get_key_info_cached(api_key)
        key_usage = self._build_key_usage_snapshot(key_info)

        # Preserve the artifact while suppressing expensive /spend/logs scans.
        output_data = {
            "trial_id": trial_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tracking_mode": "key_info_only",
            "spend_log_tracking_suppressed": True,
            "suppressed_reason": (
                "CRSBench does not query LiteLLM /spend/logs during runtime "
                "tracking; usage comes from /key/info."
            ),
            "key_alias": key_usage["key_alias"],
            "total_cost_usd_from_key": key_usage["spend"],
            "total_requests": 0,
            "logs": [],
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2, default=str))

        logger.info(
            f"Wrote LLM logs placeholder to {output_path}: "
            f"key-only spend=${float(key_usage['spend'] or 0.0):.4f}"
        )
        return output_path

    def _categorize_failure(self, log: dict) -> str:
        """Categorize a failed request log into a stable high-level reason."""
        text = json.dumps(log, default=str).lower()

        if "budgetexceedederror" in text or (
            "max budget" in text and "current cost" in text
        ):
            return "budget_exceeded"
        if (
            "auth_error" in text
            or "authentication error" in text
            or "unauthorized" in text
            or "forbidden" in text
            or "invalid api key" in text
        ):
            return "auth_error"
        if (
            "rate limit" in text
            or "too many requests" in text
            or '"status_code": 429' in text
        ):
            return "rate_limited"
        if "context length" in text or "maximum context length" in text:
            return "context_length"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if (
            '"status_code": 500' in text
            or '"status_code": 502' in text
            or '"status_code": 503' in text
            or '"status_code": 504' in text
            or "internal server error" in text
            or "service unavailable" in text
        ):
            return "server_error"
        return "unknown_error"

    def _is_failed_request(self, log: dict) -> bool:
        """Heuristic to determine if a spend log entry represents a failed call."""
        status_code = log.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            return True

        litellm_status = str(log.get("litellm_status", "")).lower()
        if litellm_status in {"failure", "failed", "error"}:
            return True

        if log.get("error") or log.get("exception") or log.get("traceback"):
            return True

        return False

    def summarize_spend_logs(self, logs: list[dict], trial_id: str) -> dict:
        """Build concise, human-readable summary data from raw spend logs."""
        by_model: dict[str, dict] = {}
        failure_categories: dict[str, int] = {}
        success_count = 0
        failure_count = 0
        total_cost_usd = 0.0

        for log in logs:
            model = str(log.get("model", "unknown"))
            spend = float(log.get("spend", 0) or 0)
            total_cost_usd += spend

            model_stats = by_model.setdefault(model, {"calls": 0, "cost_usd": 0.0})
            model_stats["calls"] += 1
            model_stats["cost_usd"] += spend

            if self._is_failed_request(log):
                failure_count += 1
                category = self._categorize_failure(log)
                failure_categories[category] = failure_categories.get(category, 0) + 1
            else:
                success_count += 1

        return {
            "trial_id": trial_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_requests": len(logs),
            "success_count": success_count,
            "failure_count": failure_count,
            "failure_categories": failure_categories,
            "total_cost_usd_from_logs": total_cost_usd,
            "by_model": by_model,
        }

    def write_llm_summary_file(
        self,
        api_key: ApiKey,
        trial_id: str,
        output_path: Path,
    ) -> Path:
        """Write concise LLM request summary to a JSON file."""
        key_info = self._get_key_info_cached(api_key)
        key_usage = self._build_key_usage_snapshot(key_info)
        summary = {
            "trial_id": trial_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tracking_mode": "key_info_only",
            "spend_log_tracking_suppressed": True,
            "key_alias": key_usage["key_alias"],
            "total_requests": 0,
            "success_count": 0,
            "failure_count": 0,
            "failure_categories": {},
            "total_cost_usd_from_logs": 0.0,
            "total_cost_usd_from_key": key_usage["spend"],
            "by_model": {},
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, default=str))

        logger.info(
            f"Wrote LLM summary placeholder to {output_path}: "
            f"key-only spend=${float(key_usage['spend'] or 0.0):.4f}"
        )
        return output_path

    def _build_key_alias(
        self,
        experiment: str,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str,
        sanitizer: str,
    ) -> KeyAlias:
        """Build a unique key alias for the trial.

        Format: crsbench-{experiment}-{crs}-{benchmark}-{harness}-{mode}-{sanitizer}-trial{N}-{random}

        The random suffix ensures uniqueness when the same experiment
        is run concurrently multiple times.

        Args:
            experiment: Experiment name
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: Build mode
            sanitizer: Sanitizer type

        Returns:
            Key alias string
        """
        import uuid

        # Sanitize components (replace problematic characters)
        def sanitize(s: str) -> str:
            return s.replace("/", "-").replace(":", "-").replace(" ", "-")

        # Use first 8 chars of UUID for uniqueness
        random_suffix = uuid.uuid4().hex[:8]

        return (
            f"crsbench-{sanitize(experiment)}-{sanitize(crs)}-"
            f"{sanitize(benchmark)}-{sanitize(harness)}-{sanitize(mode)}-{sanitize(sanitizer)}-"
            f"trial{trial_num}-{random_suffix}"
        )


class LLMTrackingContext:
    """Context manager for trial LLM tracking.

    Handles the full lifecycle of a trial's API key:
    - Generate key on entry
    - Provide key for CRS execution
    - Write usage file and delete key on exit

    Example:
        tracker = LiteLLMTracker()
        with LLMTrackingContext(
            tracker=tracker,
            experiment="exp1",
            crs="atlantis",
            benchmark="curl",
            harness="fuzz_http",
            trial_num=1,
            mode="delta",
            sanitizer="address",
            output_dir=trial_output_dir,
        ) as ctx:
            # ctx.api_key contains the trial-specific key
            run_crs_with_key(ctx.api_key)
        # Key is automatically cleaned up, usage file written
    """

    def __init__(
        self,
        tracker: LiteLLMTracker,
        experiment: str,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str,
        sanitizer: str,
        output_dir: Path,
    ):
        """Initialize tracking context.

        Args:
            tracker: LiteLLMTracker instance
            experiment: Experiment name
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: Build mode
            sanitizer: Sanitizer type
            output_dir: Directory to write llm-usage.json
        """
        self.tracker = tracker
        self.experiment = experiment
        self.crs = crs
        self.benchmark = benchmark
        self.harness = harness
        self.trial_num = trial_num
        self.mode = mode
        self.sanitizer = sanitizer
        self.output_dir = output_dir

        self.api_key: Optional[str] = None
        self.trial_id: str = ""

    def __enter__(self) -> "LLMTrackingContext":
        """Generate API key and return context."""
        self.trial_id = self.tracker._build_key_alias(
            self.experiment,
            self.crs,
            self.benchmark,
            self.harness,
            self.trial_num,
            self.mode,
            self.sanitizer,
        )

        self.api_key = self.tracker.generate_key(
            experiment=self.experiment,
            crs=self.crs,
            benchmark=self.benchmark,
            harness=self.harness,
            trial_num=self.trial_num,
            mode=self.mode,
            sanitizer=self.sanitizer,
        )

        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> bool:
        """Write usage file and cleanup key."""
        if not self.api_key:
            return False

        try:
            # Write usage file
            output_path = self.output_dir / "llm-usage.json"
            self.tracker.write_llm_usage_file(
                api_key=self.api_key,
                trial_id=self.trial_id,
                output_path=output_path,
            )
        except LiteLLMTrackerError as e:
            logger.error(f"Failed to write LLM usage file: {e}")

        try:
            # Delete key
            self.tracker.delete_key(self.api_key)
        except LiteLLMTrackerError as e:
            logger.error(f"Failed to delete LLM key: {e}")

        # Don't suppress exceptions
        return False

    def write_intermediate_usage(self) -> Optional[Path]:
        """Write intermediate usage file (for snapshots).

        Returns:
            Path to written file, or None if failed
        """
        if not self.api_key:
            return None

        try:
            output_path = self.output_dir / "llm-usage.json"
            return self.tracker.write_llm_usage_file(
                api_key=self.api_key,
                trial_id=self.trial_id,
                output_path=output_path,
            )
        except LiteLLMTrackerError as e:
            logger.warning(f"Failed to write intermediate LLM usage: {e}")
            return None


def is_tracking_available() -> bool:
    """Check if LLM tracking is available (env vars set).

    Returns:
        True if resolved tracking URL and master key are set.
    """
    runtime_env = resolve_litellm_runtime_env(
        os.environ.get("CRSBENCH_LLM_MODE", "external")
    )
    return bool(runtime_env.tracking_base_url and runtime_env.master_key)
