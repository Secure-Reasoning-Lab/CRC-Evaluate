"""Evaluator-side global verify claim worker with local DAG materialization."""

from __future__ import annotations

import base64
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from crsbench.distributed.evaluator_jobs import resolve_benchmark_path
from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    adopt_scheduler_owner_if_needed,
    build_scheduler_owner_key_for_ci_job,
    build_scheduler_owner_key_from_payload,
)
from crsbench.distributed.evaluator_verify_claims import (
    EvaluatorVerifyClaimStore,
    VerifyRequestRecord,
)
from crsbench.distributed.patch_evaluator_jobs import PatchJobPayload
from crsbench.distributed.verify_queue import (
    build_variant_rq_job_id,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

CLAIM_LEASE_SECONDS = 30
CLAIM_POLL_INTERVAL_SECONDS = 1.0
VERIFY_JOB_TIMEOUT_SECONDS = 3600
BUILD_JOB_TIMEOUT_SECONDS = 3600
DEFAULT_ADAPTIVE_HEADROOM_DECAY_SECONDS = 5.0


@dataclass(frozen=True)
class _ActiveClaim:
    local_verify_job_id: str
    required_build_job_ids: tuple[str, ...]


def build_local_ci_job_id(base_job_id: str, *, evaluator_id: str) -> str:
    """Localize an otherwise-global CI job ID to one evaluator queue."""
    suffix = f"/local/{evaluator_id}"
    if base_job_id.endswith(suffix):
        return base_job_id
    return f"{base_job_id}{suffix}"


def build_local_verify_job_id(*, request_id: str, evaluator_id: str) -> str:
    """Build an RQ-safe local wrapper job ID for one claimed verify request."""
    encoded = base64.urlsafe_b64encode(request_id.encode("utf-8")).decode("ascii")
    return f"claim-verify/{evaluator_id}/{encoded.rstrip('=')}"


def _is_job_terminal(job: Any | None) -> bool:
    if job is None:
        return True
    status = job.get_status()
    return status in {"finished", "failed", "stopped", "canceled", "cancelled"}


def _refresh_terminal_job(queue: Any, *, job_id: str, existing: Any) -> Any | None:
    """Best-effort delete of a terminal job; fail if a terminal wrapper still remains."""
    try:
        from crsbench.distributed.queue import remove_job_by_id

        remove_job_by_id(queue, job_id)
    except Exception:
        pass

    jobs = getattr(queue, "jobs", None)
    if isinstance(jobs, dict):
        jobs.pop(job_id, None)

    delete = getattr(existing, "delete", None)
    if callable(delete):
        try:
            delete()
        except Exception:
            pass

    remaining = queue.fetch_job(job_id)
    if remaining is None or not _is_job_terminal(remaining):
        return remaining

    raise RuntimeError(f"Failed to remove terminal job {job_id} before refresh")


def _enqueue_or_reuse_job(
    queue: Any,
    func_name: str,
    payload: dict[str, Any],
    *,
    job_timeout: int,
    job_id: str,
    meta: dict[str, Any],
    depends_on: list[Any] | None = None,
    refresh_terminal: bool = False,
) -> Any:
    existing = queue.fetch_job(job_id)
    if existing is not None:
        if refresh_terminal and _is_job_terminal(existing):
            existing = _refresh_terminal_job(queue, job_id=job_id, existing=existing)
        if existing is not None:
            adopt_scheduler_owner_if_needed(
                existing,
                new_owner=meta.get(SCHEDULER_OWNER_KEY_META),
            )
            return existing
    try:
        return queue.enqueue(
            func_name,
            payload,
            job_timeout=job_timeout,
            result_ttl=-1,
            job_id=job_id,
            depends_on=depends_on,
            meta=meta,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" not in msg and not ("job id" in msg and "exists" in msg):
            raise
        existing = queue.fetch_job(job_id)
        if existing is None:
            raise
        if refresh_terminal and _is_job_terminal(existing):
            existing = _refresh_terminal_job(queue, job_id=job_id, existing=existing)
            if existing is None:
                return _enqueue_or_reuse_job(
                    queue,
                    func_name,
                    payload,
                    job_timeout=job_timeout,
                    job_id=job_id,
                    meta=meta,
                    depends_on=depends_on,
                    refresh_terminal=refresh_terminal,
                )
        adopt_scheduler_owner_if_needed(
            existing,
            new_owner=meta.get(SCHEDULER_OWNER_KEY_META),
        )
        return existing


class EvaluatorClaimWorker:
    """Claim logical verify work fairly and turn it into local RQ DAGs."""

    def __init__(
        self,
        *,
        redis_conn: Any,
        experiment_name: str,
        evaluator_id: str,
        build_queue: Any,
        verify_queue: Any,
        verification_engine: Any,
        benchmarks_root: Path,
        claim_lease_seconds: int = CLAIM_LEASE_SECONDS,
        max_inflight_requests: int = 1,
        claim_batch_size: int = 1,
        local_verify_capacity: int | None = None,
        buffered_claim_max_hold_seconds: float | None = None,
        adaptive_headroom_decay_seconds: float = (
            DEFAULT_ADAPTIVE_HEADROOM_DECAY_SECONDS
        ),
    ) -> None:
        self.store = EvaluatorVerifyClaimStore(
            redis_conn,
            experiment_name=experiment_name,
        )
        self.experiment_name = experiment_name
        self.evaluator_id = evaluator_id
        self.build_queue = build_queue
        self.verify_queue = verify_queue
        self.verification_engine = verification_engine
        self.benchmarks_root = benchmarks_root
        self.claim_lease_seconds = max(1, int(claim_lease_seconds))
        self.max_inflight_requests = max(1, int(max_inflight_requests))
        self.claim_batch_size = max(1, int(claim_batch_size))
        self.local_verify_capacity = max(
            1,
            int(local_verify_capacity or self.max_inflight_requests),
        )
        if buffered_claim_max_hold_seconds is None:
            buffered_claim_max_hold_seconds = float(self.claim_lease_seconds)
        self.buffered_claim_max_hold_seconds = max(
            0.0,
            float(buffered_claim_max_hold_seconds),
        )
        self.adaptive_headroom_decay_seconds = max(
            0.0,
            float(adaptive_headroom_decay_seconds),
        )
        self._active_claims: dict[str, _ActiveClaim] = {}
        self._active_claims_lock = threading.Lock()
        self._warmup_enqueue_gate = threading.Lock()
        self._claimed_request_buffer: deque[VerifyRequestRecord] = deque()
        self._claimed_request_buffered_at: dict[str, float] = {}
        self._adaptive_extra_headroom = 0
        self._last_refill_miss_at: float | None = None
        self._pending_verify_capacity_open = 0
        self._wake_event = threading.Event()

    def notify_verify_capacity_opened(self) -> None:
        self._pending_verify_capacity_open += 1
        self._wake_event.set()

    def wait_for_claim_work(
        self,
        stop_event: threading.Event,
        *,
        poll_interval_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + max(float(poll_interval_seconds), 0.0)
        while True:
            if stop_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._wake_event.wait(timeout=min(remaining, 0.1)):
                self._wake_event.clear()
                return stop_event.is_set()

    def _current_outstanding_target(self) -> int:
        return self.max_inflight_requests + self._adaptive_extra_headroom

    def _outstanding_claim_count(self) -> int:
        return len(self._active_claims) + len(self._claimed_request_buffer)

    def _record_buffered_claims(
        self,
        claimed_records: list[VerifyRequestRecord],
        *,
        now: float,
    ) -> None:
        for claimed in claimed_records:
            self._claimed_request_buffer.append(claimed)
            self._claimed_request_buffered_at[claimed.request_id] = now

    def _pop_buffered_claim(self) -> VerifyRequestRecord | None:
        if not self._claimed_request_buffer:
            return None
        claimed = self._claimed_request_buffer.popleft()
        self._claimed_request_buffered_at.pop(claimed.request_id, None)
        return claimed

    def _release_claimed_request(self, *, request_id: str) -> bool:
        self._claimed_request_buffered_at.pop(request_id, None)
        return self.store.release_claim_if_current(
            request_id=request_id,
            evaluator_id=self.evaluator_id,
        )

    def _apply_adaptive_headroom_decay(self, *, now: float) -> None:
        if (
            self._adaptive_extra_headroom <= 0
            or self.adaptive_headroom_decay_seconds <= 0
            or self._last_refill_miss_at is None
        ):
            return
        while (
            self._adaptive_extra_headroom > 0
            and now - self._last_refill_miss_at >= self.adaptive_headroom_decay_seconds
        ):
            self._adaptive_extra_headroom -= 1
            if self._adaptive_extra_headroom <= 0:
                self._adaptive_extra_headroom = 0
                self._last_refill_miss_at = None
                return
            self._last_refill_miss_at += self.adaptive_headroom_decay_seconds

    def _maybe_record_refill_miss(
        self,
        *,
        now: float,
        claimed_any: bool,
        buffer_was_empty: bool,
    ) -> None:
        if self._pending_verify_capacity_open > 0 and buffer_was_empty and claimed_any:
            self._adaptive_extra_headroom = min(
                self.local_verify_capacity,
                self._adaptive_extra_headroom + 1,
            )
            self._last_refill_miss_at = now
        self._pending_verify_capacity_open = 0

    def _active_claim_snapshot(self) -> tuple[set[str], tuple[_ActiveClaim, ...]]:
        with self._active_claims_lock:
            return set(self._active_claims), tuple(self._active_claims.values())

    def _active_claims_items(self) -> tuple[tuple[str, _ActiveClaim], ...]:
        with self._active_claims_lock:
            return tuple(self._active_claims.items())

    def _register_active_claim(
        self,
        *,
        request_id: str,
        active: _ActiveClaim | None,
    ) -> None:
        with self._active_claims_lock:
            if active is not None:
                self._active_claims[request_id] = active

    def _has_claimed_request_pending_activation(
        self, *, active_request_ids: set[str]
    ) -> bool:
        for record in self.store.list_requests():
            if record.request_id in active_request_ids:
                continue
            claim = record.claim
            if claim is None or record.terminal_result is not None:
                continue
            if claim.expires_at <= time.time():
                continue
            if claim.evaluator_id == self.evaluator_id:
                return True
        return False

    def has_pending_required_builds(self) -> bool:
        active_request_ids, active_claims = self._active_claim_snapshot()
        if self._has_claimed_request_pending_activation(
            active_request_ids=active_request_ids
        ):
            return True
        for active in active_claims:
            for build_job_id in active.required_build_job_ids:
                if not _is_job_terminal(self.build_queue.fetch_job(build_job_id)):
                    return True
        return False

    def enqueue_warmup_build_if_idle(
        self,
        *,
        build_queue: Any,
        spec: Any,
    ) -> bool:
        with self._warmup_enqueue_gate:
            if self.has_pending_required_builds():
                return False
            build_queue.enqueue(
                "crsbench.distributed.build_jobs.execute_ci_build",
                spec.payload,
                job_timeout=BUILD_JOB_TIMEOUT_SECONDS,
                result_ttl=-1,
                job_id=spec.job_id,
                meta=dict(spec.meta),
            )
            return True

    def refresh_active_claims(self, *, now: float) -> None:
        completed: list[str] = []
        for request_id, active in self._active_claims_items():
            record = self.store.load_request(request_id)
            if record is None or record.terminal_result is not None:
                completed.append(request_id)
                continue
            local_verify_job = self.verify_queue.fetch_job(active.local_verify_job_id)
            if _is_job_terminal(local_verify_job):
                released = self.store.release_claim_if_current(
                    request_id=request_id,
                    evaluator_id=self.evaluator_id,
                )
                if released:
                    logger.warning(
                        "Released logical verify claim after local verify job {} ended without publishing request={}",
                        active.local_verify_job_id,
                        request_id,
                    )
                completed.append(request_id)
                continue
            renewed = self.store.renew_claim(
                request_id=request_id,
                evaluator_id=self.evaluator_id,
                now=now,
                lease_seconds=self.claim_lease_seconds,
            )
            if not renewed:
                completed.append(request_id)
        if completed:
            with self._active_claims_lock:
                for request_id in completed:
                    self._active_claims.pop(request_id, None)

        self._apply_adaptive_headroom_decay(now=now)

        if self._claimed_request_buffer:
            retained_buffer: deque[VerifyRequestRecord] = deque()
            active_count = len(self._active_claims)
            allowed_buffered = max(0, self._current_outstanding_target() - active_count)
            for claimed in self._claimed_request_buffer:
                buffered_at = self._claimed_request_buffered_at.get(
                    claimed.request_id,
                    now,
                )
                if len(retained_buffer) >= allowed_buffered or (
                    now - buffered_at > self.buffered_claim_max_hold_seconds
                ):
                    self._release_claimed_request(request_id=claimed.request_id)
                    continue
                record = self.store.load_request(claimed.request_id)
                if record is None or record.terminal_result is not None:
                    self._claimed_request_buffered_at.pop(claimed.request_id, None)
                    continue
                renewed = self.store.renew_claim(
                    request_id=claimed.request_id,
                    evaluator_id=self.evaluator_id,
                    now=now,
                    lease_seconds=self.claim_lease_seconds,
                )
                if renewed:
                    retained_buffer.append(claimed)
                else:
                    self._claimed_request_buffered_at.pop(claimed.request_id, None)
            self._claimed_request_buffer = retained_buffer

    def dispatch_one(self, *, now: float) -> VerifyRequestRecord | None:
        # `dispatch_one()` only runs on the claim-loop thread; cross-thread
        # coordination is limited to warmup reads of active/materializing state.
        if len(self._active_claims) >= self._current_outstanding_target():
            return None
        claimed = self._pop_next_claimed_request(now=now)
        if claimed is None:
            return None
        try:
            active = self._materialize_claimed_request(claimed)
        except Exception:
            released = self.store.release_claim_if_current(
                request_id=claimed.request_id,
                evaluator_id=self.evaluator_id,
            )
            logger.exception(
                "Failed to materialize claimed verify request {}; released_claim={}",
                claimed.request_id,
                released,
            )
            return None
        self._register_active_claim(
            request_id=claimed.request_id,
            active=active,
        )
        return claimed

    def _claim_request_batch(self, *, now: float) -> list[VerifyRequestRecord]:
        buffer_was_empty = not self._claimed_request_buffer
        target_gap = (
            self._current_outstanding_target() - self._outstanding_claim_count()
        )
        if target_gap <= 0:
            self._pending_verify_capacity_open = 0
            return []
        claim_limit = min(
            self.claim_batch_size,
            self.local_verify_capacity,
            target_gap,
        )
        with self._warmup_enqueue_gate:
            if claim_limit <= 1:
                claimed = self.store.claim_next_request(
                    evaluator_id=self.evaluator_id,
                    now=now,
                    lease_seconds=self.claim_lease_seconds,
                )
                claimed_records = [] if claimed is None else [claimed]
            else:
                claimed_records = self.store.claim_next_requests(
                    evaluator_id=self.evaluator_id,
                    now=now,
                    lease_seconds=self.claim_lease_seconds,
                    limit=claim_limit,
                )
        self._maybe_record_refill_miss(
            now=now,
            claimed_any=bool(claimed_records),
            buffer_was_empty=buffer_was_empty,
        )
        return claimed_records

    def _pop_next_claimed_request(self, *, now: float) -> VerifyRequestRecord | None:
        claimed = self._pop_buffered_claim()
        if claimed is not None:
            return claimed
        claimed_batch = self._claim_request_batch(now=now)
        if not claimed_batch:
            return None
        self._record_buffered_claims(claimed_batch[1:], now=now)
        return claimed_batch[0]

    def dispatch_available(self, *, now: float) -> VerifyRequestRecord | None:
        """Claim as many logical requests as the local inflight limit allows."""
        first_claimed: VerifyRequestRecord | None = None
        while True:
            claimed = self.dispatch_one(now=now)
            if claimed is None:
                break
            if first_claimed is None:
                first_claimed = claimed
        return first_claimed

    def tick(self, *, now: float) -> VerifyRequestRecord | None:
        self.refresh_active_claims(now=now)
        return self.dispatch_available(now=now)

    def _materialize_claimed_request(
        self,
        record: VerifyRequestRecord,
    ) -> _ActiveClaim | None:
        if record.request_kind == "patch":
            return self._materialize_patch_request(record)
        return self._materialize_pov_request(record)

    def _materialize_pov_request(
        self, record: VerifyRequestRecord
    ) -> _ActiveClaim | None:
        from crsbench.benchmark_ci.jobs.flat import (
            BuildSingleVariantJob,
            PrepareIncImageJob,
        )
        from crsbench.distributed.ci_jobs import serialize_ci_job
        from crsbench.distributed.evaluator_claim_jobs import ClaimedVerifyPayload
        from crsbench.distributed.evaluator_jobs import SinglePovPayload

        payload = SinglePovPayload.from_dict(record.payload)
        benchmark_path = resolve_benchmark_path(self.benchmarks_root, payload.benchmark)
        adapter = self.verification_engine.load_adapter(benchmark_path)
        if adapter is None:
            raise ValueError(
                f"Failed to load adapter for benchmark={payload.benchmark}"
            )

        resolved_sanitizer = payload.sanitizer
        if resolved_sanitizer is None:
            sanitizers = adapter.get_all_cpv_sanitizers()
            resolved_sanitizer = sanitizers[0] if sanitizers else "address"

        source_mode = self.verification_engine.builder.source_mode
        plan = self.verification_engine.builder.create_build_plan(
            benchmark_name=adapter.benchmark_name,
            benchmark_path=benchmark_path,
            main_repo=adapter.main_repo,
            mode=adapter.get_mode(),
            base_commit=adapter.get_base_commit(),
            ref_commit=adapter.get_ref_commit(),
            cpv_numbers=adapter.get_cpv_numbers(),
            language=adapter.lang,
            repo_name=adapter.repo_name,
            include_coverage=False,
            use_inc_build=payload.use_inc_build,
            sanitizer=resolved_sanitizer,
        )

        prepare_dependency: list[Any] = []
        if payload.use_inc_build:
            prepare_job = PrepareIncImageJob(
                benchmark_path=benchmark_path,
                benchmark_name=adapter.benchmark_name,
                sanitizer=resolved_sanitizer,
                use_inc_build=True,
                source_mode=source_mode,
                inc_image_policy=self.verification_engine.builder.infra.inc_image_policy,
                inc_image_registry=self.verification_engine.builder.infra.inc_image_registry,
                inc_image_max_pull_bytes=self.verification_engine.builder.infra.inc_image_max_pull_bytes,
                inc_image_pull_timeout=self.verification_engine.builder.infra.inc_image_pull_timeout,
                local_image_prefix=self.verification_engine.builder.infra.local_image_prefix,
            )
            prepare_job_id = build_local_ci_job_id(
                prepare_job.job_id,
                evaluator_id=self.evaluator_id,
            )
            prepare_meta = {"experiment_name": self.experiment_name}
            prepare_meta[SCHEDULER_OWNER_KEY_META] = (
                build_scheduler_owner_key_for_ci_job(
                    prepare_job,
                    experiment_name=self.experiment_name,
                )
            )
            prepare_rq_job = _enqueue_or_reuse_job(
                self.build_queue,
                "crsbench.distributed.build_jobs.execute_ci_build",
                serialize_ci_job(prepare_job),
                job_timeout=BUILD_JOB_TIMEOUT_SECONDS,
                job_id=prepare_job_id,
                meta=prepare_meta,
            )
            if not _is_job_terminal(prepare_rq_job):
                prepare_dependency = [prepare_rq_job]
        local_build_ids: list[str] = []
        verify_dependencies: list[Any] = []
        required_build_ids: list[str] = []
        for config in plan.configs:
            build_job = BuildSingleVariantJob(
                benchmark_path=config.benchmark_path,
                benchmark_name=config.benchmark_name,
                variant_type=config.variant_type,
                commit=config.commit,
                main_repo=config.main_repo,
                mode=config.mode or adapter.get_mode(),
                language=config.language,
                cpv_num=config.cpv_num,
                patch_id=config.patch_id,
                pov_id=config.pov_id,
                patches=config.patches,
                use_inc_build=config.use_inc_build,
                source_mode=source_mode,
                sanitizer=config.sanitizer,
                repo_name=config.repo_name,
                prepare_inc_job_id=prepare_dependency[0].id
                if prepare_dependency
                else "",
                inc_image_policy=self.verification_engine.builder.infra.inc_image_policy,
                inc_image_registry=self.verification_engine.builder.infra.inc_image_registry,
                inc_image_max_pull_bytes=self.verification_engine.builder.infra.inc_image_max_pull_bytes,
                inc_image_pull_timeout=self.verification_engine.builder.infra.inc_image_pull_timeout,
                local_image_prefix=self.verification_engine.builder.infra.local_image_prefix,
            )
            base_job_id = build_variant_rq_job_id(
                benchmark=config.benchmark_name,
                variant_name=config.variant_name,
                source_mode=source_mode,
                use_inc_build=config.use_inc_build,
            )
            local_job_id = build_local_ci_job_id(
                base_job_id,
                evaluator_id=self.evaluator_id,
            )
            local_build_ids.append(local_job_id)
            build_meta = {"experiment_name": self.experiment_name}
            build_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_for_ci_job(
                build_job,
                experiment_name=self.experiment_name,
            )
            build_rq_job = _enqueue_or_reuse_job(
                self.build_queue,
                "crsbench.distributed.build_jobs.execute_ci_build",
                serialize_ci_job(build_job),
                job_timeout=BUILD_JOB_TIMEOUT_SECONDS,
                job_id=local_job_id,
                meta=build_meta,
                depends_on=prepare_dependency or None,
            )
            if not _is_job_terminal(build_rq_job):
                verify_dependencies.append(build_rq_job)
                required_build_ids.append(local_job_id)

        local_payload = payload.to_dict()
        local_payload["build_job_ids"] = list(local_build_ids)
        local_payload["build_artifact_ids"] = list(local_build_ids)

        verify_meta = {"experiment_name": self.experiment_name}
        verify_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_from_payload(
            local_payload,
            fallback_job_id=record.request_id,
            queue_name=getattr(self.verify_queue, "name", "verify"),
        )
        wrapper_payload = ClaimedVerifyPayload(
            experiment_name=self.experiment_name,
            request_id=record.request_id,
            evaluator_id=self.evaluator_id,
            request_kind="pov",
            verify_payload=local_payload,
        )
        local_verify_job_id = build_local_verify_job_id(
            request_id=record.request_id,
            evaluator_id=self.evaluator_id,
        )
        verify_rq_job = _enqueue_or_reuse_job(
            self.verify_queue,
            "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
            wrapper_payload.__dict__,
            job_timeout=VERIFY_JOB_TIMEOUT_SECONDS,
            job_id=local_verify_job_id,
            meta=verify_meta,
            depends_on=verify_dependencies or None,
            refresh_terminal=True,
        )
        return _ActiveClaim(
            local_verify_job_id=verify_rq_job.id,
            required_build_job_ids=tuple(required_build_ids),
        )

    def _materialize_patch_request(
        self, record: VerifyRequestRecord
    ) -> _ActiveClaim | None:
        from crsbench.distributed.evaluator_claim_jobs import ClaimedVerifyPayload
        from crsbench.distributed.patch_queue import (
            _make_patch_build_rq_job_id,
            _patch_content_hash,
        )

        payload = PatchJobPayload.from_dict(record.payload)
        patch_hash = _patch_content_hash(payload.patch.patch_content_b64)
        base_build_job_id = _make_patch_build_rq_job_id(
            experiment_name=payload.experiment_name,
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            cpv_id=payload.cpv_id,
            patch_id=payload.patch.patch_id,
            sanitizer=payload.sanitizer,
            source_mode=payload.source_mode,
            use_inc_build=payload.use_inc_build,
            patch_content_hash=patch_hash,
        )
        local_build_job_id = build_local_ci_job_id(
            base_build_job_id,
            evaluator_id=self.evaluator_id,
        )
        build_meta = {"experiment_name": self.experiment_name}
        build_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_from_payload(
            payload.to_dict(),
            fallback_job_id=f"{payload.benchmark}/{payload.harness}/{payload.cpv_id}/{payload.patch.patch_id}",
            queue_name=getattr(self.build_queue, "name", "build"),
        )
        build_rq_job = _enqueue_or_reuse_job(
            self.build_queue,
            "crsbench.distributed.patch_evaluator_jobs.execute_patch_build",
            payload.to_dict(),
            job_timeout=BUILD_JOB_TIMEOUT_SECONDS,
            job_id=local_build_job_id,
            meta=build_meta,
        )

        local_payload = payload.to_dict()
        local_payload["build_patch_job_id"] = local_build_job_id
        verify_meta = {"experiment_name": self.experiment_name}
        verify_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_from_payload(
            local_payload,
            fallback_job_id=record.request_id,
            queue_name=getattr(self.verify_queue, "name", "verify"),
        )
        wrapper_payload = ClaimedVerifyPayload(
            experiment_name=self.experiment_name,
            request_id=record.request_id,
            evaluator_id=self.evaluator_id,
            request_kind="patch",
            verify_payload=local_payload,
        )
        local_verify_job_id = build_local_verify_job_id(
            request_id=record.request_id,
            evaluator_id=self.evaluator_id,
        )
        verify_rq_job = _enqueue_or_reuse_job(
            self.verify_queue,
            "crsbench.distributed.evaluator_claim_jobs.execute_claimed_verify",
            wrapper_payload.__dict__,
            job_timeout=VERIFY_JOB_TIMEOUT_SECONDS,
            job_id=local_verify_job_id,
            meta=verify_meta,
            depends_on=[build_rq_job] if not _is_job_terminal(build_rq_job) else None,
            refresh_terminal=True,
        )
        required_build_ids = (
            () if _is_job_terminal(build_rq_job) else (local_build_job_id,)
        )
        return _ActiveClaim(
            local_verify_job_id=verify_rq_job.id,
            required_build_job_ids=required_build_ids,
        )


def start_claim_thread(
    worker: EvaluatorClaimWorker,
    *,
    poll_interval_seconds: float = CLAIM_POLL_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    """Start the evaluator-side logical verify claim loop."""
    stop_event = threading.Event()

    def _loop() -> None:
        while True:
            try:
                worker.tick(now=time.time())
            except Exception:
                logger.exception("Evaluator claim loop iteration failed")
            if worker.wait_for_claim_work(
                stop_event,
                poll_interval_seconds=poll_interval_seconds,
            ):
                return

    thread = threading.Thread(
        target=_loop,
        name=f"claim-loop-{worker.evaluator_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
