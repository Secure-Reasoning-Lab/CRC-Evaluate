"""Redis-backed dispatcher state for evaluator-routed build/verify jobs."""

from __future__ import annotations

import json
import time
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

_PUBLISH_RESULT_IF_CURRENT_SCRIPT = """
local attempts_key = KEYS[1]
local results_key = KEYS[2]
local request_id = ARGV[1]
local expected_attempt_id = ARGV[2]
local result_payload = ARGV[3]

local current_attempt_id = redis.call('HGET', attempts_key, request_id)
if current_attempt_id ~= expected_attempt_id then
    return 0
end

redis.call('HSET', results_key, request_id, result_payload)
return 1
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


@dataclass(frozen=True)
class BuildResultRecord:
    request_id: str
    attempt_id: str
    generation: int
    evaluator_id: str
    terminal_state: str


class DispatcherStateRedisProtocol(Protocol):
    """Synchronous Redis operations used by the dispatcher state store."""

    def hset(self, key: str, field: str, value: str) -> object: ...

    def hget(self, key: str, field: str) -> str | bytes | None: ...

    def hdel(self, key: str, field: str) -> int: ...

    def hgetall(self, key: str) -> dict[str, str | bytes]: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


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

    def _build_results_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:build_results"

    def _build_attempts_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:build_attempts"

    def _verify_attempts_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:verify_attempts"

    def _evaluators_key(self) -> str:
        return f"crsbench:dispatcher:{self.experiment_name}:evaluators"

    def submit_build_request(self, record: BuildRequestRecord) -> str:
        existing = self.load_build_request(record.request_id)
        if existing is not None and (
            existing.trial_id != record.trial_id
            or existing.benchmark != record.benchmark
            or existing.owner_key != record.owner_key
            or existing.lineage_id != record.lineage_id
        ):
            raise ValueError(
                f"conflicting build request identity for {record.request_id}"
            )
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

    def assign_build_attempt(
        self,
        *,
        request_id: str,
        evaluator_id: str,
        attempt_id: str,
        generation: int,
    ) -> None:
        record = self.load_build_request(request_id)
        if record is None:
            raise ValueError(f"unknown build request: {request_id}")
        payload = dict(record.payload)
        payload["attempt_id"] = attempt_id
        payload["evaluator_id"] = evaluator_id
        self.redis.hset(self._build_attempts_key(), request_id, attempt_id)
        self.submit_build_request(
            BuildRequestRecord(
                request_id=record.request_id,
                trial_id=record.trial_id,
                benchmark=record.benchmark,
                owner_key=record.owner_key,
                lineage_id=record.lineage_id,
                generation=generation,
                state="running",
                payload=payload,
            )
        )

    def build_attempt_is_current(self, request_id: str, attempt_id: str) -> bool:
        current_attempt_id = self.redis.hget(self._build_attempts_key(), request_id)
        if current_attempt_id is None:
            return False
        return _decode(current_attempt_id) == attempt_id

    def load_build_result(self, request_id: str) -> BuildResultRecord | None:
        raw = self.redis.hget(self._build_results_key(), request_id)
        if raw is None:
            return None
        payload = json.loads(_decode(raw))
        return BuildResultRecord(**payload)

    def publish_build_result(self, request_id: str, result: BuildResultRecord) -> None:
        if request_id != result.request_id:
            raise ValueError(
                "publish_build_result request_id does not match result.request_id"
            )
        self.redis.hset(
            self._build_results_key(),
            request_id,
            json.dumps(asdict(result), sort_keys=True),
        )

    def publish_build_result_if_current(
        self,
        *,
        request_id: str,
        attempt_id: str,
        result: BuildResultRecord,
    ) -> bool:
        if request_id != result.request_id:
            raise ValueError(
                "publish_build_result_if_current request_id does not match "
                "result.request_id"
            )
        published = self.redis.eval(
            _PUBLISH_RESULT_IF_CURRENT_SCRIPT,
            2,
            self._build_attempts_key(),
            self._build_results_key(),
            request_id,
            attempt_id,
            json.dumps(asdict(result), sort_keys=True),
        )
        return bool(published)

    def upsert_evaluator(
        self,
        *,
        evaluator_id: str,
        worker_name: str,
        expires_in_seconds: int,
    ) -> None:
        expires_at = time.time() + expires_in_seconds
        self.redis.hset(
            self._evaluators_key(),
            evaluator_id,
            json.dumps(
                {
                    "evaluator_id": evaluator_id,
                    "worker_name": worker_name,
                    "expires_at": expires_at,
                },
                sort_keys=True,
            ),
        )

    def list_live_evaluators(self, *, now: float) -> list[str]:
        live: list[str] = []
        for evaluator_id, raw in self.redis.hgetall(self._evaluators_key()).items():
            payload = json.loads(_decode(raw))
            if float(payload.get("expires_at", 0.0)) < now:
                continue
            live.append(_decode(evaluator_id))
        return sorted(live)

    def submit_verify_request(self, record: VerifyRequestRecord) -> str:
        existing = self.load_verify_request(record.request_id)
        if existing is not None and (
            existing.trial_id != record.trial_id
            or existing.benchmark != record.benchmark
            or existing.harness != record.harness
            or existing.pov_id != record.pov_id
            or existing.owner_key != record.owner_key
            or existing.lineage_id != record.lineage_id
        ):
            raise ValueError(
                f"conflicting verify request identity for {record.request_id}"
            )
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

    def list_verify_requests(self) -> list[VerifyRequestRecord]:
        values = self.redis.hgetall(self._verify_requests_key()).values()
        return [VerifyRequestRecord(**json.loads(_decode(raw))) for raw in values]

    def assign_verify_attempt(
        self,
        *,
        request_id: str,
        evaluator_id: str,
        attempt_id: str,
        generation: int,
    ) -> None:
        record = self.load_verify_request(request_id)
        if record is None:
            raise ValueError(f"unknown verify request: {request_id}")
        payload = dict(record.payload)
        payload["attempt_id"] = attempt_id
        payload["evaluator_id"] = evaluator_id
        self.redis.hset(self._verify_attempts_key(), request_id, attempt_id)
        self.submit_verify_request(
            VerifyRequestRecord(
                request_id=record.request_id,
                trial_id=record.trial_id,
                benchmark=record.benchmark,
                harness=record.harness,
                pov_id=record.pov_id,
                owner_key=record.owner_key,
                lineage_id=record.lineage_id,
                generation=generation,
                state="running",
                build_request_ids=list(record.build_request_ids),
                payload=payload,
            )
        )

    def verify_attempt_is_current(self, request_id: str, attempt_id: str) -> bool:
        current_attempt_id = self.redis.hget(self._verify_attempts_key(), request_id)
        if current_attempt_id is None:
            return False
        return _decode(current_attempt_id) == attempt_id

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

    def publish_verify_result_if_current(
        self,
        *,
        request_id: str,
        attempt_id: str,
        result: VerifyResultRecord,
    ) -> bool:
        if request_id != result.request_id:
            raise ValueError(
                "publish_verify_result_if_current request_id does not match "
                "result.request_id"
            )
        published = self.redis.eval(
            _PUBLISH_RESULT_IF_CURRENT_SCRIPT,
            2,
            self._verify_attempts_key(),
            self._verify_results_key(),
            request_id,
            attempt_id,
            json.dumps(asdict(result), sort_keys=True),
        )
        return bool(published)

    def _all_builds_succeeded(self, request_ids: list[str], generation: int) -> bool:
        for request_id in request_ids:
            result = self.load_build_result(request_id)
            if result is None:
                return False
            if result.generation != generation:
                return False
            if result.terminal_state != "succeeded":
                return False
        return True

    def promote_ready_verify_requests(
        self, *, lineage_id: str, generation: int
    ) -> None:
        for request in self.list_verify_requests():
            if request.lineage_id != lineage_id:
                continue
            if request.generation != generation:
                continue
            if request.state != "blocked_on_build":
                continue
            if not self._all_builds_succeeded(request.build_request_ids, generation):
                continue
            self.submit_verify_request(
                VerifyRequestRecord(
                    request_id=request.request_id,
                    trial_id=request.trial_id,
                    benchmark=request.benchmark,
                    harness=request.harness,
                    pov_id=request.pov_id,
                    owner_key=request.owner_key,
                    lineage_id=request.lineage_id,
                    generation=request.generation,
                    state="ready",
                    build_request_ids=list(request.build_request_ids),
                    payload=dict(request.payload),
                )
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
        if len(raw_results) != len(request_ids):
            raise ValueError(
                "dispatcher verify poll returned "
                f"{len(raw_results)} results for {len(request_ids)} request ids"
            )
        for request_id, raw in zip(request_ids, raw_results, strict=False):
            if raw is None:
                remaining.append(request_id)
                continue
            payload = json.loads(_decode(raw))
            record = VerifyResultRecord(**payload)
            completed.append(record.verdict)
        return completed, remaining
