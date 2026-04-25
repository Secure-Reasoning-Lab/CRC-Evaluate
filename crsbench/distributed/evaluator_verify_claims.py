"""Redis-backed global verify intake and claim tracking."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

try:
    from redis.exceptions import WatchError
except ImportError:  # pragma: no cover - redis is available in distributed envs

    class WatchError(Exception):
        pass


from crsbench.distributed.queue import validate_queue_name_component


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _next_owner(owner_order: list[str], *, last_owner: str | None) -> str:
    if not owner_order:
        raise ValueError("owner_order must be non-empty")
    if last_owner not in owner_order:
        return owner_order[0]
    index = owner_order.index(last_owner)
    return owner_order[(index + 1) % len(owner_order)]


@dataclass(frozen=True)
class VerifyClaim:
    evaluator_id: str
    expires_at: float


@dataclass(frozen=True)
class VerifyRequestRecord:
    request_id: str
    owner_key: str
    request_kind: str
    payload: dict[str, Any]
    claim: VerifyClaim | None = None
    terminal_result: dict[str, Any] | None = None


class VerifyClaimRedisProtocol(Protocol):
    """Synchronous Redis hash operations used by the claim store."""

    def hset(self, key: str, field: str, value: str) -> object: ...

    def hget(self, key: str, field: str) -> str | bytes | None: ...

    def hgetall(self, key: str) -> dict[str, str | bytes]: ...

    def hdel(self, key: str, field: str) -> int: ...

    def pipeline(self) -> Any: ...


class EvaluatorVerifyClaimStore:
    """Persist logical verify requests and evaluator claim leases."""

    def __init__(
        self, redis_conn: VerifyClaimRedisProtocol, *, experiment_name: str
    ) -> None:
        validate_queue_name_component(experiment_name)
        self.redis = redis_conn
        self.experiment_name = experiment_name

    def _requests_key(self) -> str:
        return f"crsbench:verify-claims:{self.experiment_name}:requests"

    def _cursor_key(self) -> str:
        return f"crsbench:verify-claims:{self.experiment_name}:cursor"

    def _serialize_record(self, record: VerifyRequestRecord) -> str:
        return json.dumps(asdict(record), sort_keys=True)

    def _deserialize_record(self, raw: str | bytes) -> VerifyRequestRecord:
        payload = json.loads(_decode(raw))
        claim_payload = payload.get("claim")
        claim = (
            VerifyClaim(**claim_payload) if isinstance(claim_payload, dict) else None
        )
        return VerifyRequestRecord(
            request_id=payload["request_id"],
            owner_key=payload["owner_key"],
            request_kind=payload["request_kind"],
            payload=payload["payload"],
            claim=claim,
            terminal_result=payload.get("terminal_result"),
        )

    def _sorted_records_from_mapping(
        self, mapping: dict[str, str | bytes]
    ) -> list[VerifyRequestRecord]:
        records = [self._deserialize_record(raw) for raw in mapping.values()]
        return sorted(records, key=lambda record: record.request_id)

    def submit_request(self, record: VerifyRequestRecord) -> str:
        existing = self.load_request(record.request_id)
        if existing is not None and (
            existing.owner_key != record.owner_key
            or existing.request_kind != record.request_kind
        ):
            raise ValueError(
                f"conflicting verify request identity for {record.request_id}"
            )
        self.redis.hset(
            self._requests_key(),
            record.request_id,
            self._serialize_record(record),
        )
        return record.request_id

    def load_request(self, request_id: str) -> VerifyRequestRecord | None:
        raw = self.redis.hget(self._requests_key(), request_id)
        if raw is None:
            return None
        return self._deserialize_record(raw)

    def list_requests(self) -> list[VerifyRequestRecord]:
        return self._sorted_records_from_mapping(
            self.redis.hgetall(self._requests_key())
        )

    def claim_next_request(
        self,
        *,
        evaluator_id: str,
        now: float,
        lease_seconds: int,
    ) -> VerifyRequestRecord | None:
        requests_key = self._requests_key()
        cursor_key = self._cursor_key()
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(requests_key, cursor_key)
                    records = self._sorted_records_from_mapping(
                        pipe.hgetall(requests_key)
                    )
                    eligible = [
                        record
                        for record in records
                        if record.terminal_result is None
                        and (record.claim is None or record.claim.expires_at <= now)
                    ]
                    if not eligible:
                        pipe.unwatch()
                        return None

                    owner_order = list(
                        dict.fromkeys(record.owner_key for record in eligible)
                    )
                    raw_last_owner = pipe.hget(cursor_key, "last_owner")
                    last_owner = (
                        _decode(raw_last_owner) if raw_last_owner is not None else None
                    )
                    selected_owner = _next_owner(owner_order, last_owner=last_owner)
                    selected = next(
                        record
                        for record in eligible
                        if record.owner_key == selected_owner
                    )
                    claimed = VerifyRequestRecord(
                        request_id=selected.request_id,
                        owner_key=selected.owner_key,
                        request_kind=selected.request_kind,
                        payload=dict(selected.payload),
                        claim=VerifyClaim(
                            evaluator_id=evaluator_id,
                            expires_at=now + lease_seconds,
                        ),
                        terminal_result=selected.terminal_result,
                    )
                    pipe.multi()
                    pipe.hset(
                        requests_key,
                        claimed.request_id,
                        self._serialize_record(claimed),
                    )
                    pipe.hset(cursor_key, "last_owner", selected_owner)
                    pipe.execute()
                    return claimed
            except WatchError:
                continue

    def publish_result(self, *, request_id: str, result: dict[str, Any]) -> None:
        record = self.load_request(request_id)
        if record is None:
            raise ValueError(f"unknown verify request: {request_id}")
        self.submit_request(
            VerifyRequestRecord(
                request_id=record.request_id,
                owner_key=record.owner_key,
                request_kind=record.request_kind,
                payload=dict(record.payload),
                claim=None,
                terminal_result=result,
            )
        )

    def renew_claim(
        self,
        *,
        request_id: str,
        evaluator_id: str,
        now: float,
        lease_seconds: int,
    ) -> bool:
        requests_key = self._requests_key()
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(requests_key)
                    raw = pipe.hget(requests_key, request_id)
                    if raw is None:
                        pipe.unwatch()
                        return False
                    record = self._deserialize_record(raw)
                    if (
                        record.claim is None
                        or record.terminal_result is not None
                        or record.claim.evaluator_id != evaluator_id
                        or record.claim.expires_at <= now
                    ):
                        pipe.unwatch()
                        return False
                    renewed = VerifyRequestRecord(
                        request_id=record.request_id,
                        owner_key=record.owner_key,
                        request_kind=record.request_kind,
                        payload=dict(record.payload),
                        claim=VerifyClaim(
                            evaluator_id=evaluator_id,
                            expires_at=now + lease_seconds,
                        ),
                        terminal_result=None,
                    )
                    pipe.multi()
                    pipe.hset(
                        requests_key,
                        renewed.request_id,
                        self._serialize_record(renewed),
                    )
                    pipe.execute()
                    return True
            except WatchError:
                continue

    def release_claim_if_current(
        self,
        *,
        request_id: str,
        evaluator_id: str,
    ) -> bool:
        requests_key = self._requests_key()
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(requests_key)
                    raw = pipe.hget(requests_key, request_id)
                    if raw is None:
                        pipe.unwatch()
                        return False
                    record = self._deserialize_record(raw)
                    if (
                        record.claim is None
                        or record.terminal_result is not None
                        or record.claim.evaluator_id != evaluator_id
                    ):
                        pipe.unwatch()
                        return False
                    released = VerifyRequestRecord(
                        request_id=record.request_id,
                        owner_key=record.owner_key,
                        request_kind=record.request_kind,
                        payload=dict(record.payload),
                        claim=None,
                        terminal_result=None,
                    )
                    pipe.multi()
                    pipe.hset(
                        requests_key,
                        released.request_id,
                        self._serialize_record(released),
                    )
                    pipe.execute()
                    return True
            except WatchError:
                continue

    def publish_result_if_current(
        self,
        *,
        request_id: str,
        evaluator_id: str,
        now: float,
        result: dict[str, Any],
    ) -> bool:
        requests_key = self._requests_key()
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(requests_key)
                    raw = pipe.hget(requests_key, request_id)
                    if raw is None:
                        pipe.unwatch()
                        return False
                    record = self._deserialize_record(raw)
                    if (
                        record.claim is None
                        or record.terminal_result is not None
                        or record.claim.evaluator_id != evaluator_id
                        or record.claim.expires_at <= now
                    ):
                        pipe.unwatch()
                        return False
                    published = VerifyRequestRecord(
                        request_id=record.request_id,
                        owner_key=record.owner_key,
                        request_kind=record.request_kind,
                        payload=dict(record.payload),
                        claim=None,
                        terminal_result=result,
                    )
                    pipe.multi()
                    pipe.hset(
                        requests_key,
                        published.request_id,
                        self._serialize_record(published),
                    )
                    pipe.execute()
                    return True
            except WatchError:
                continue

    def poll_results(
        self, request_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        completed: list[dict[str, Any]] = []
        remaining: list[str] = []
        for request_id in request_ids:
            record = self.load_request(request_id)
            if record is None or record.terminal_result is None:
                remaining.append(request_id)
                continue
            completed.append(record.terminal_result)
        return completed, remaining
