"""Accounting adapter for OSS-CRS internal LiteLLM spend reports."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class OssCrsSpendSnapshot:
    """Validated cumulative spend from an OSS-CRS report."""

    total_cost_usd: float
    by_crs: dict[str, float]
    updated_at: Optional[int]


class OssCrsSpendReport:
    """Read OSS-CRS spend reports and write CRSBench accounting artifacts."""

    def __init__(
        self,
        report_path: Path,
        *,
        trial_id: str,
        max_budget_usd: Optional[float] = None,
    ) -> None:
        self.report_path = report_path
        self.trial_id = trial_id
        self.max_budget_usd = max_budget_usd
        self._last_snapshot: Optional[OssCrsSpendSnapshot] = None
        self._lock = threading.Lock()

    @staticmethod
    def _cost(value: Any) -> float:
        cost = float(value)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("LiteLLM spend must be a finite non-negative number")
        return cost

    @classmethod
    def _parse(cls, raw: Any) -> OssCrsSpendSnapshot:
        if not isinstance(raw, dict):
            raise ValueError("OSS-CRS spend report must be a JSON object")

        raw_crs = raw.get("crs") or {}
        if not isinstance(raw_crs, dict):
            raise ValueError("OSS-CRS spend report crs field must be an object")
        by_crs: dict[str, float] = {}
        for name, entry in raw_crs.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                raise ValueError("OSS-CRS spend report contains an invalid CRS entry")
            by_crs[name] = cls._cost(entry.get("credits_used", 0.0))

        totals = raw.get("totals") or {}
        if not isinstance(totals, dict):
            raise ValueError("OSS-CRS spend report totals field must be an object")
        total_value = totals.get("credits_used")
        total = sum(by_crs.values()) if total_value is None else cls._cost(total_value)

        updated_at = raw.get("updated_at")
        if updated_at is not None:
            updated_at = int(updated_at)

        return OssCrsSpendSnapshot(
            total_cost_usd=total,
            by_crs=by_crs,
            updated_at=updated_at,
        )

    def read(self) -> Optional[OssCrsSpendSnapshot]:
        """Return the latest valid non-decreasing spend snapshot."""
        with self._lock:
            try:
                raw = json.loads(self.report_path.read_text(encoding="utf-8"))
                candidate = self._parse(raw)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.debug(
                    "Unable to read OSS-CRS LiteLLM spend report {}: {}",
                    self.report_path,
                    exc,
                )
                return self._last_snapshot

            if (
                self._last_snapshot is not None
                and candidate.total_cost_usd < self._last_snapshot.total_cost_usd
            ):
                return self._last_snapshot
            self._last_snapshot = candidate
            return candidate

    def budget_state(self) -> Optional[tuple[float, float]]:
        """Return cumulative spend and configured budget when both are available."""
        if self.max_budget_usd is None:
            return None
        snapshot = self.read()
        if snapshot is None:
            return None
        return snapshot.total_cost_usd, self.max_budget_usd

    def write_usage_file(self, output_path: Path) -> Optional[Path]:
        """Write a CRSBench-compatible llm-usage.json from the latest report."""
        snapshot = self.read()
        if snapshot is None:
            return None

        timestamp = (
            datetime.fromtimestamp(snapshot.updated_at, tz=timezone.utc)
            if snapshot.updated_at is not None
            else datetime.now(timezone.utc)
        )
        output = {
            "trial_id": self.trial_id,
            "timestamp": timestamp.isoformat(),
            "total_cost_usd": snapshot.total_cost_usd,
            "key_alias": "oss-crs-internal",
            "key_info": {
                "spend": snapshot.total_cost_usd,
                "max_budget": self.max_budget_usd,
                "by_crs": snapshot.by_crs,
                "source": "oss-crs",
            },
            "total_api_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cache_hits": 0,
            "total_cache_misses": 0,
            "by_model": {},
            "request_count": 0,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        temporary_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        temporary_path.replace(output_path)
        logger.info(
            "Wrote OSS-CRS LiteLLM usage to {}: ${:.4f}",
            output_path,
            snapshot.total_cost_usd,
        )
        return output_path
