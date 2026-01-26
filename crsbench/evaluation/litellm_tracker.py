"""LiteLLM usage tracking via Virtual Keys API.

This module provides functionality to track LLM usage per trial using
LiteLLM's Virtual Keys feature. Each trial gets a unique API key, enabling
accurate per-trial cost and token tracking.

Reference:
    - LiteLLM Virtual Keys: https://docs.litellm.ai/docs/proxy/virtual_keys
    - LiteLLM Spend Tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
    - LiteLLM Spend Logs: https://docs.litellm.ai/docs/proxy/spend_logs
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

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
            result.update(self.detailed_usage.to_dict())
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
        LITELLM_BASE_URL: LiteLLM proxy URL (e.g., http://litellm:4000)
        LITELLM_MASTER_KEY: Master key for key management APIs
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        master_key: Optional[str] = None,
        *,
        timeout: int = 30,
    ):
        """Initialize LiteLLM tracker.

        Args:
            base_url: LiteLLM proxy URL. Defaults to LITELLM_BASE_URL or
                UPSTREAM_LITELLM_BASE_URL env var.
            master_key: Master key for API. Defaults to LITELLM_MASTER_KEY env var.
            timeout: Request timeout in seconds.

        Raises:
            LiteLLMTrackerError: If required environment variables are not set.
        """
        # Try LITELLM_BASE_URL first, then fall back to UPSTREAM_LITELLM_BASE_URL
        self.base_url = (
            base_url
            or os.environ.get("LITELLM_BASE_URL")
            or os.environ.get("UPSTREAM_LITELLM_BASE_URL")
        )
        self.master_key = master_key or os.environ.get("LITELLM_MASTER_KEY")
        self.timeout = timeout

        if not self.base_url:
            raise LiteLLMTrackerError(
                "LITELLM_BASE_URL or UPSTREAM_LITELLM_BASE_URL not set. "
                "Required for LLM tracking."
            )
        if not self.master_key:
            raise LiteLLMTrackerError(
                "LITELLM_MASTER_KEY not set. Required for LLM tracking."
            )

        # Remove trailing slash from base URL
        self.base_url = self.base_url.rstrip("/")

        self._headers = {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }

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
            response = requests.post(
                f"{self.base_url}/key/generate",
                headers=self._headers,
                json=payload,
                timeout=self.timeout,
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
            response = requests.get(
                f"{self.base_url}/key/info",
                headers=self._headers,
                params={"key": api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Retrieved key info for {api_key[:20]}...")
            return data

        except requests.RequestException as e:
            raise LiteLLMTrackerError(f"Failed to get key info: {e}") from e

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
            response = requests.post(
                f"{self.base_url}/key/delete",
                headers=self._headers,
                json={"keys": [api_key]},
                timeout=self.timeout,
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

            response = requests.get(
                f"{self.base_url}/spend/logs",
                headers=self._headers,
                params=params,
                timeout=self.timeout,
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
            include_detailed_logs: Whether to fetch and include detailed spend logs

        Returns:
            LLMUsageData with usage information including detailed metrics

        Raises:
            LiteLLMTrackerError: If usage query fails
        """
        key_info = self.get_key_info(api_key)

        # Extract spend from key info
        # LiteLLM stores spend in the 'info' object
        info = key_info.get("info", {})
        spend = info.get("spend", 0.0)
        key_alias = info.get("key_alias", "unknown")

        # Get detailed usage from spend logs
        detailed_usage = None
        if include_detailed_logs:
            logs = self.get_spend_logs(api_key)
            if logs:
                detailed_usage = self.aggregate_spend_logs(logs)
                # Use detailed cost if available (more accurate)
                if detailed_usage.total_cost_usd > 0:
                    spend = detailed_usage.total_cost_usd
                logger.info(
                    f"Aggregated {len(logs)} LLM requests: "
                    f"{detailed_usage.total_api_calls} calls, "
                    f"{detailed_usage.total_tokens} tokens, "
                    f"${detailed_usage.total_cost_usd:.4f}"
                )

        return LLMUsageData(
            trial_id=trial_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_spend_usd=spend,
            key_alias=key_alias,
            key_info={
                "key_alias": key_alias,
                "spend": spend,
                "max_budget": info.get("max_budget"),
                "metadata": info.get("metadata", {}),
            },
            raw_response=key_info,
            detailed_usage=detailed_usage,
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
        # Get all spend logs for this API key
        logs = self.get_spend_logs(api_key)

        # Build output structure with metadata and raw logs
        output_data = {
            "trial_id": trial_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_requests": len(logs),
            "logs": logs,  # Store all raw log entries with full details
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2, default=str))

        logger.info(f"Wrote LLM logs to {output_path}: {len(logs)} requests")
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
        True if (LITELLM_BASE_URL or UPSTREAM_LITELLM_BASE_URL) and LITELLM_MASTER_KEY are set
    """
    base_url = os.environ.get("LITELLM_BASE_URL") or os.environ.get(
        "UPSTREAM_LITELLM_BASE_URL"
    )
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    return bool(base_url and master_key)
