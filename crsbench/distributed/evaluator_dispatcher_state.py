"""Redis-backed dispatcher state for evaluator-routed build/verify jobs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from crsbench.distributed.queue import validate_queue_name_component

_POLL_VERIFY_RESULTS_SCRIPT = """
local key = KEYS[1]
local results = {}
for i, field in ipairs(ARGV) do
    local value = redis.call('HGET', key, field)
    results[i] = value
    if value then
        redis.call('HDEL', key, field)
    end
end
return results
"""


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


@dataclass(frozen=True)
class BuildRequestRecord:
    request_id: str
    trial_id: str
    benchmark: str
    owner_key: str
    lineage_id: str
    generation: int
    state: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class VerifyRequestRecord:
    request_id: str
    trial_id: str
    benchmark: str
    harness: str
    pov_id: str
    owner_key: str
    lineage_id: str
    generation: int
    state: str
    build_request_ids: list[str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class VerifyResultRecord:
    request_id: str
    attempt_id: str
    verdict: dict[str, Any]
    terminal_state: str


class DispatcherStateRedisProtocol(Protocol):
    """Synchronous Redis operations used by the dispatcher state store."""

    def hset(self, key: str, field: str, value: str) -> object: ...

    def hget(self, key: str, field: str) -> str | bytes | None: ...

    def eval(
        self, script: str, numkeys: int, *keys_and_args: str
    ) -> list[str | bytes | None]: ...


class DispatcherStateStore:
    """Persist evaluator dispatcher requests/results in Redis."""

    def __init__(
        self, redis_conn: DispatcherStateRedisProtocol, *, experiment_name: str
    ) -> None:
        validate_queue_name_component(experiment_name)
        self.redis = redis_conn
        self.experiment_name = experiment_name

    def _build_requests_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:build_requests"

    def _verify_requests_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:verify_requests"

    def _verify_results_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:verify_results"

    def submit_build_request(self, record: BuildRequestRecord) -> str:
        payload = asdict(record)
        self.redis.hset(
            self._build_requests_key(),
            record.request_id,
            json.dumps(payload, sort_keys=True),
        )
        return record.request_id

    def load_build_request(self, request_id: str) -> BuildRequestRecord | None:
        raw = self.redis.hget(self._build_requests_key(), request_id)
        if raw is None:
            return None
        payload = json.loads(_decode(raw))
        return BuildRequestRecord(**payload)

    def submit_verify_request(self, record: VerifyRequestRecord) -> str:
        payload = asdict(record)
        self.redis.hset(
            self._verify_requests_key(),
            record.request_id,
            json.dumps(payload, sort_keys=True),
        )
        return record.request_id

    def load_verify_request(self, request_id: str) -> VerifyRequestRecord | None:
        raw = self.redis.hget(self._verify_requests_key(), request_id)
        if raw is None:
            return None
        payload = json.loads(_decode(raw))
        return VerifyRequestRecord(**payload)

    def publish_verify_result(
        self, request_id: str, result: VerifyResultRecord
    ) -> None:
        if request_id != result.request_id:
            raise ValueError(
                "publish_verify_result request_id does not match result.request_id"
            )
        self.redis.hset(
            self._verify_results_key(),
            request_id,
            json.dumps(asdict(result), sort_keys=True),
        )

    def poll_verify_results(
        self, request_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not request_ids:
            return [], []
        completed: list[dict[str, Any]] = []
        remaining: list[str] = []
        key = self._verify_results_key()
        raw_results = self.redis.eval(
            _POLL_VERIFY_RESULTS_SCRIPT,
            1,
            key,
            *request_ids,
        )
        for request_id, raw in zip(request_ids, raw_results, strict=False):
            if raw is None:
                remaining.append(request_id)
                continue
            payload = json.loads(_decode(raw))
            record = VerifyResultRecord(**payload)
            completed.append(record.verdict)
        return completed, remaining
