"""Evaluator-side global verify claim worker with local DAG materialization."""

from __future__ import annotations

import base64
import threading
import time
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


def _enqueue_or_reuse_job(
    queue: Any,
    func_name: str,
    payload: dict[str, Any],
    *,
    job_timeout: int,
    job_id: str,
    meta: dict[str, Any],
    depends_on: list[Any] | None = None,
) -> Any:
    existing = queue.fetch_job(job_id)
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
        self._active_claims: dict[str, _ActiveClaim] = {}
        self._active_claims_lock = threading.Lock()
        self._dispatch_slot_tokens: set[int] = set()
        self._next_dispatch_slot_token = 0

    def _active_claims_items(self) -> tuple[tuple[str, _ActiveClaim], ...]:
        with self._active_claims_lock:
            return tuple(self._active_claims.items())

    def _reserve_dispatch_slot(self) -> int | None:
        with self._active_claims_lock:
            inflight = len(self._active_claims) + len(self._dispatch_slot_tokens)
            if inflight >= self.max_inflight_requests:
                return None
            dispatch_slot_token = self._next_dispatch_slot_token
            self._next_dispatch_slot_token += 1
            self._dispatch_slot_tokens.add(dispatch_slot_token)
            return dispatch_slot_token

    def _release_dispatch_slot(self, dispatch_slot_token: int) -> None:
        with self._active_claims_lock:
            self._dispatch_slot_tokens.discard(dispatch_slot_token)

    def _register_active_claim(
        self,
        *,
        request_id: str,
        active: _ActiveClaim | None,
        dispatch_slot_token: int,
    ) -> None:
        with self._active_claims_lock:
            if active is not None:
                self._active_claims[request_id] = active
            self._dispatch_slot_tokens.discard(dispatch_slot_token)

    def has_pending_required_builds(self) -> bool:
        with self._active_claims_lock:
            # A claimed request that is still materializing may enqueue required
            # local builds, so warmup must treat reserved slots as pending demand.
            if self._dispatch_slot_tokens:
                return True
            active_claims = tuple(self._active_claims.values())
        for active in active_claims:
            for build_job_id in active.required_build_job_ids:
                if not _is_job_terminal(self.build_queue.fetch_job(build_job_id)):
                    return True
        return False

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

    def dispatch_one(self, *, now: float) -> VerifyRequestRecord | None:
        dispatch_slot_token = self._reserve_dispatch_slot()
        if dispatch_slot_token is None:
            return None
        try:
            claimed = self.store.claim_next_request(
                evaluator_id=self.evaluator_id,
                now=now,
                lease_seconds=self.claim_lease_seconds,
            )
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
                dispatch_slot_token=dispatch_slot_token,
            )
            return claimed
        finally:
            self._release_dispatch_slot(dispatch_slot_token)

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
            if stop_event.wait(poll_interval_seconds):
                return

    thread = threading.Thread(
        target=_loop,
        name=f"claim-loop-{worker.evaluator_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
