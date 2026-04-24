"""Verify queue utilities for async POV verification.

This module provides functions for workers to enqueue POV verification
jobs and poll for results. The verify queue is consumed by evaluator
processes running on the same or different machines.

All verification uses per-POV granularity (verify_single_pov) for
fine-grained parallelism and individual retry.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, cast

from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    adopt_scheduler_owner_if_needed,
    build_scheduler_owner_key_for_ci_job,
    build_scheduler_owner_key_from_payload,
)
from crsbench.distributed.queue import (
    REDIS_AVAILABLE,
    ROUTING_MODEL_DISPATCHER,
    get_evaluator_routing_model,
)
from crsbench.utils.logger import get_logger

if REDIS_AVAILABLE:
    import rq
    import rq.job

if TYPE_CHECKING:
    from crsbench.distributed.evaluator_dispatcher_state import (
        DispatcherStateRedisProtocol,
        DispatcherStateStore,
    )

logger = get_logger(__name__)

# Maximum POV size to enqueue (10MB). Larger POVs are skipped with a warning.
MAX_POV_SIZE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class AsyncPovBuildPrereqs:
    logical_build_request_ids: list[str]
    artifact_build_ids: list[str]
    rq_dependencies: list[object]
    sanitizer: str


def _build_verify_request_id(
    *, trial_id: str, benchmark: str, harness: str, pov_id: str
) -> str:
    return f"verify:{trial_id}:{benchmark}:{harness}:{pov_id}"


def build_dispatcher_build_request_id(
    *, trial_id: str, benchmark: str, index: int
) -> str:
    return f"build:{trial_id}:{benchmark}:{index}"


def _build_lineage_id(
    benchmark: str,
    sanitizer: Optional[str],
    source_mode: str,
    use_inc_build: bool,
) -> str:
    sanitizer_value = sanitizer or "auto"
    inc_build_label = "inc" if use_inc_build else "clean"
    return f"{benchmark}::{sanitizer_value}::{source_mode}::{inc_build_label}"


def _get_dispatcher_store(
    *,
    redis_host: Optional[str],
    experiment_name: str,
    redis_conn: Optional[object] = None,
) -> "DispatcherStateStore":
    from crsbench.distributed.evaluator_dispatcher_state import DispatcherStateStore

    if redis_conn is None:
        if redis_host is None:
            raise ValueError("redis_host is required when redis_conn is not provided")
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
    return DispatcherStateStore(
        cast("DispatcherStateRedisProtocol", redis_conn),
        experiment_name=experiment_name,
    )


def _get_verify_claim_store(
    *,
    redis_host: Optional[str],
    experiment_name: str,
    redis_conn: Optional[object] = None,
):
    from crsbench.distributed.evaluator_verify_claims import (
        EvaluatorVerifyClaimStore,
    )

    if redis_conn is None:
        if redis_host is None:
            raise ValueError("redis_host is required when redis_conn is not provided")
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
    return EvaluatorVerifyClaimStore(
        cast("Any", redis_conn),
        experiment_name=experiment_name,
    )


def submit_async_build_requests(
    *,
    redis_host: Optional[str],
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    build_payloads: list[dict[str, Any]],
    sanitizer: Optional[str],
    source_mode: str,
    use_inc_build: bool,
    redis_conn: Optional[object] = None,
) -> list[str]:
    """Submit logical build requests for async POV verification."""
    if not build_payloads:
        return []
    if not experiment_name:
        raise ValueError("experiment_name is required for dispatcher build requests")
    if not trial_id:
        raise ValueError("trial_id is required for dispatcher build requests")

    store = _get_dispatcher_store(
        redis_host=redis_host,
        experiment_name=experiment_name,
        redis_conn=redis_conn,
    )
    owner_key = f"trial::{experiment_name}::{trial_id}"
    lineage_id = _build_lineage_id(benchmark, sanitizer, source_mode, use_inc_build)

    from crsbench.distributed.evaluator_dispatcher_state import BuildRequestRecord

    request_ids: list[str] = []
    for index, payload in enumerate(build_payloads):
        request_id = build_dispatcher_build_request_id(
            trial_id=trial_id,
            benchmark=benchmark,
            index=index,
        )
        record = BuildRequestRecord(
            request_id=request_id,
            trial_id=trial_id,
            benchmark=benchmark,
            owner_key=owner_key,
            lineage_id=lineage_id,
            generation=1,
            state="ready",
            payload=payload,
        )
        store.submit_build_request(record)
        request_ids.append(request_id)
    return request_ids


def prepare_async_pov_build_prereqs(
    *,
    redis_host: Optional[str],
    experiment_name: str,
    trial_id: str,
    engine: Any,
    adapter: Any,
    build_queue: Optional[Any],
    sanitizer: Optional[str],
    use_inc_build: bool,
) -> AsyncPovBuildPrereqs | None:
    """Prepare explicit build prerequisites for async POV verification."""
    try:
        _ = (redis_host, trial_id)
        from crsbench.benchmark_ci.jobs.flat import (
            BuildSingleVariantJob,
            PrepareIncImageJob,
        )

        benchmark_path = getattr(adapter, "benchmark_path", None)
        if benchmark_path is None:
            logger.warning(
                "MetaYamlAdapter missing benchmark path, cannot enqueue builds"
            )
            return None

        resolved_sanitizer = sanitizer
        if resolved_sanitizer is None:
            sanitizers = adapter.get_all_cpv_sanitizers()
            resolved_sanitizer = sanitizers[0] if sanitizers else "address"

        source_mode = engine.builder.source_mode
        plan = engine.builder.create_build_plan(
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
            use_inc_build=use_inc_build,
            sanitizer=resolved_sanitizer,
        )

        if get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER:
            artifact_build_ids: list[str] = []
            prepare_request_id = ""
            if use_inc_build:
                prepare_job = PrepareIncImageJob(
                    benchmark_path=benchmark_path,
                    benchmark_name=adapter.benchmark_name,
                    sanitizer=resolved_sanitizer,
                    use_inc_build=True,
                    source_mode=source_mode,
                    inc_image_policy=engine.builder.infra.inc_image_policy,
                    inc_image_registry=engine.builder.infra.inc_image_registry,
                    inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                    inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                    local_image_prefix=engine.builder.infra.local_image_prefix,
                )
                prepare_request_id = prepare_job.job_id

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
                    prepare_inc_job_id=prepare_request_id,
                    inc_image_policy=engine.builder.infra.inc_image_policy,
                    inc_image_registry=engine.builder.infra.inc_image_registry,
                    inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                    inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                    local_image_prefix=engine.builder.infra.local_image_prefix,
                )
                artifact_build_ids.append(build_job.job_id)
            return AsyncPovBuildPrereqs(
                logical_build_request_ids=list(artifact_build_ids),
                artifact_build_ids=list(artifact_build_ids),
                rq_dependencies=[],
                sanitizer=resolved_sanitizer,
            )

        if build_queue is None:
            logger.warning("Build queue not available, skipping async POV enqueue")
            return None

        prepare_dependency: list[object] = []
        if use_inc_build:
            prepare_job = PrepareIncImageJob(
                benchmark_path=benchmark_path,
                benchmark_name=adapter.benchmark_name,
                sanitizer=resolved_sanitizer,
                use_inc_build=True,
                source_mode=source_mode,
                inc_image_policy=engine.builder.infra.inc_image_policy,
                inc_image_registry=engine.builder.infra.inc_image_registry,
                inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                local_image_prefix=engine.builder.infra.local_image_prefix,
            )
            prepare_rq_job = enqueue_ci_job(
                build_queue,
                experiment_name,
                prepare_job,
            )
            prepare_dependency = [prepare_rq_job]

        build_job_ids: list[str] = []
        build_dependencies: list[object] = []
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
                inc_image_policy=engine.builder.infra.inc_image_policy,
                inc_image_registry=engine.builder.infra.inc_image_registry,
                inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                local_image_prefix=engine.builder.infra.local_image_prefix,
            )
            rq_job_id = build_variant_rq_job_id(
                benchmark=config.benchmark_name,
                variant_name=config.variant_name,
                source_mode=source_mode,
                use_inc_build=config.use_inc_build,
            )
            build_rq_job = enqueue_ci_job(
                build_queue,
                experiment_name,
                build_job,
                depends_on=prepare_dependency or None,
                job_id=rq_job_id,
            )
            build_job_ids.append(build_rq_job.id)
            build_dependencies.append(build_rq_job)

        return AsyncPovBuildPrereqs(
            logical_build_request_ids=list(build_job_ids),
            artifact_build_ids=list(build_job_ids),
            rq_dependencies=list(build_dependencies),
            sanitizer=resolved_sanitizer,
        )
    except Exception as e:
        logger.warning(f"Failed to enqueue async POV build DAG: {e}")
        return None


def _is_duplicate_job_enqueue_error(exc: Exception) -> bool:
    """Best-effort duplicate enqueue detection across RQ versions."""
    msg = str(exc).lower()
    return "already exists" in msg or "job id" in msg and "exists" in msg


def _enqueue_with_existing_reuse(
    queue: Any,
    job_func: str,
    payload: dict[str, Any],
    *,
    job_timeout: int,
    meta: dict[str, str],
    job_id: str,
    depends_on: Optional[list[Any]] = None,
) -> Any:
    """Enqueue an RQ job by deterministic ID and reuse existing duplicates."""
    try:
        return queue.enqueue(
            job_func,
            payload,
            job_timeout=job_timeout,
            result_ttl=-1,
            job_id=job_id,
            depends_on=depends_on,
            meta=meta,
        )
    except Exception as e:
        if not _is_duplicate_job_enqueue_error(e):
            raise
        existing = rq.job.Job.fetch(job_id, connection=queue.connection)
        adopt_scheduler_owner_if_needed(
            existing,
            new_owner=meta.get(SCHEDULER_OWNER_KEY_META),
        )
        logger.debug(f"Reusing existing POV RQ job {job_id}")
        return existing


def _error_result_from_rq_job(job: Any, *, default_error: str) -> dict[str, Any]:
    """Build a terminal error verdict preserving routing metadata."""
    raw_args = getattr(job, "args", None)
    raw_kwargs = getattr(job, "kwargs", None)
    payload: dict[str, Any] = {}
    if isinstance(raw_args, dict):
        payload = raw_args
    elif (
        isinstance(raw_args, (list, tuple))
        and raw_args
        and isinstance(raw_args[0], dict)
    ):
        payload = raw_args[0]
    elif isinstance(raw_kwargs, dict):
        payload = raw_kwargs
    pov_payload = payload.get("pov")
    pov_id = (
        pov_payload.get("pov_id", "unknown")
        if isinstance(pov_payload, dict)
        else "unknown"
    )
    return {
        "trial_id": payload.get("trial_id", ""),
        "benchmark": payload.get("benchmark", ""),
        "harness": payload.get("harness", ""),
        "verdict": {
            "pov_id": pov_id,
            "triggered_bug": False,
            "status": "error",
            "cpv_matches": [],
            "error": default_error[:500],
        },
        "completed_at": time.time(),
    }


def _normalize_dispatcher_result(
    request_id: str, result_dict: dict[str, Any]
) -> dict[str, Any]:
    """Normalize dispatcher results to the shared SinglePovResult dict shape."""
    if {
        "trial_id",
        "benchmark",
        "harness",
        "verdict",
        "completed_at",
    } <= result_dict.keys():
        return result_dict

    prefix, trial_id, benchmark, harness, pov_id = request_id.split(":", 4)
    if prefix != "verify":
        raise ValueError(f"invalid dispatcher verify request id: {request_id}")

    verdict = dict(result_dict)
    verdict.setdefault("pov_id", pov_id)
    verdict.setdefault("triggered_bug", False)
    verdict.setdefault("cpv_matches", [])
    verdict.setdefault("variant_results", {})
    verdict.setdefault("crash_logs", {})
    if not verdict.get("status"):
        verdict["status"] = "error"
        verdict["error"] = (
            verdict.get("error")
            or f"Dispatcher result for {request_id} is missing verdict status"
        )
    else:
        verdict.setdefault("error", None)

    return {
        "trial_id": trial_id,
        "benchmark": benchmark,
        "harness": harness,
        "verdict": verdict,
        "completed_at": result_dict.get("completed_at", time.time()),
    }


def initialize_verify_queue(
    redis_host: str, experiment_name: str
) -> Optional["rq.Queue"]:
    """Initialize the verification queue for an experiment.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming

    Returns:
        RQ Queue instance, or None if Redis unavailable
    """
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed, verify queue unavailable")
        return None

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, _build_queue, queue_name = resolve_queue_names(experiment_name)
    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Verify queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize verify queue: {e}")
        return None


def initialize_build_queue(
    redis_host: str, experiment_name: str
) -> Optional["rq.Queue"]:
    """Initialize the build queue used by async POV verification."""
    if not REDIS_AVAILABLE:
        logger.debug("Redis/RQ packages not installed, build queue unavailable")
        return None

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, queue_name, _verify_queue = resolve_queue_names(experiment_name)
    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)
        queue = rq.Queue(queue_name, connection=redis_conn)
        logger.info(f"Build queue initialized: {queue_name}")
        return queue
    except Exception as e:
        logger.warning(f"Failed to initialize build queue: {e}")
        return None


def build_variant_rq_job_id(
    *,
    benchmark: str,
    variant_name: str,
    source_mode: str,
    use_inc_build: bool,
) -> str:
    """Build a deterministic RQ job ID for async POV build prerequisites."""
    mode = "inc" if use_inc_build else "clean"
    return f"build-single/{benchmark}/{variant_name}/{source_mode}/{mode}"


def enqueue_ci_job(
    queue: Any,
    experiment_name: str,
    job: Any,
    *,
    job_timeout: int = 3600,
    cpu_tag: Optional[str] = None,
    depends_on: Optional[list[Any]] = None,
    job_id: Optional[str] = None,
) -> Any:
    """Enqueue a serialized CI job with deterministic reuse semantics."""
    from crsbench.distributed.ci_jobs import serialize_ci_job

    effective_cpu_tag = cpu_tag or os.environ.get("CRSBENCH_JOB_CPU_TAG")
    job_meta = {"experiment_name": experiment_name}
    if effective_cpu_tag:
        job_meta["cpu_tag"] = effective_cpu_tag
    job_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_for_ci_job(
        job,
        experiment_name=experiment_name,
    )

    resolved_job_id = job_id or getattr(job, "job_id", "")
    if not resolved_job_id:
        raise ValueError("enqueue_ci_job requires a deterministic job_id")

    return _enqueue_with_existing_reuse(
        queue,
        "crsbench.distributed.ci_jobs.execute_ci_job",
        serialize_ci_job(job),
        job_timeout=job_timeout,
        meta=job_meta,
        job_id=resolved_job_id,
        depends_on=depends_on,
    )


def enqueue_single_pov(
    verify_queue: Optional["rq.Queue"],
    experiment_name: str,
    trial_id: str,
    benchmark: str,
    harness: str,
    pov_id: str,
    pov_data: bytes,
    *,
    sanitizer: Optional[str] = None,
    job_timeout: int = 3600,
    cpu_tag: Optional[str] = None,
    build_job_ids: Optional[list[str]] = None,
    build_artifact_ids: Optional[list[str]] = None,
    depends_on: Optional[list[Any]] = None,
    source_mode: str = "pkgs",
    use_inc_build: bool = True,
    redis_host: Optional[str] = None,
    redis_conn: Optional[object] = None,
) -> Optional[str]:
    """Enqueue a single POV for async verification.

    Used by POVVerificationManager in async mode to enqueue individual
    POVs as they are discovered during CRS execution.

    Args:
        verify_queue: RQ verify queue instance
        experiment_name: Experiment identifier
        trial_id: Trial identifier for result correlation
        benchmark: Benchmark name
        harness: Harness name
        pov_id: POV identifier (filename)
        pov_data: Raw POV file content
        sanitizer: Sanitizer scope for selecting verification builds
        job_timeout: Job execution timeout in seconds

    Returns:
        Job ID if enqueued, None on error
    """
    if len(pov_data) > MAX_POV_SIZE_BYTES:
        logger.warning(
            f"Skipping POV {pov_id}: {len(pov_data)} bytes exceeds "
            f"{MAX_POV_SIZE_BYTES} byte limit"
        )
        return None

    from crsbench.distributed.evaluator_jobs import (
        EmbeddedPov,
        SinglePovPayload,
    )

    embedded_pov = EmbeddedPov.from_bytes(pov_id, pov_data)
    payload = SinglePovPayload(
        experiment_name=experiment_name,
        trial_id=trial_id,
        benchmark=benchmark,
        harness=harness,
        pov=embedded_pov,
        enqueued_at=time.time(),
        sanitizer=sanitizer,
        build_job_ids=list(build_job_ids or []),
        build_artifact_ids=list(build_artifact_ids or []),
        source_mode=source_mode,
        use_inc_build=use_inc_build,
    )

    if get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER:
        if not experiment_name:
            logger.warning("Skipping dispatcher POV enqueue: missing experiment_name")
            return None
        if not trial_id:
            logger.warning("Skipping dispatcher POV enqueue: missing trial_id")
            return None

        owner_key = f"trial::{experiment_name}::{trial_id}"
        request_id = _build_verify_request_id(
            trial_id=trial_id,
            benchmark=benchmark,
            harness=harness,
            pov_id=pov_id,
        )

        from crsbench.distributed.evaluator_verify_claims import VerifyRequestRecord

        try:
            store = _get_verify_claim_store(
                redis_host=redis_host,
                experiment_name=experiment_name,
                redis_conn=redis_conn,
            )
        except ValueError as e:
            logger.warning(f"Skipping dispatcher POV enqueue: {e}")
            return None
        record = VerifyRequestRecord(
            request_id=request_id,
            owner_key=owner_key,
            request_kind="pov",
            payload=payload.to_dict(),
        )
        store.submit_request(record)
        logger.debug(
            "Submitted logical POV verify request {} for {}/{} pov={}",
            request_id,
            benchmark,
            harness,
            pov_id,
        )
        return request_id

    try:
        effective_cpu_tag = cpu_tag or os.environ.get("CRSBENCH_JOB_CPU_TAG")
        job_meta = {"experiment_name": experiment_name}
        if effective_cpu_tag:
            job_meta["cpu_tag"] = effective_cpu_tag
        job_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_from_payload(
            payload.to_dict(),
            fallback_job_id=f"{benchmark}/{harness}/{pov_id}",
            queue_name=getattr(verify_queue, "name", "verify"),
        )

        if verify_queue is None:
            raise ValueError("verify_queue is required for shared routing")

        job = verify_queue.enqueue(
            "crsbench.distributed.evaluator_jobs.verify_single_pov",
            payload.to_dict(),
            job_timeout=job_timeout,
            result_ttl=-1,
            depends_on=depends_on,
            meta=job_meta,
        )
        logger.debug(
            f"Enqueued single POV verify job {job.id[:8]} "
            f"for {benchmark}/{harness} pov={pov_id}"
        )
        return job.id
    except Exception as e:
        logger.warning(f"Failed to enqueue single POV verify job: {e}")
        return None


def poll_single_pov_verdicts(
    redis_host: str,
    job_ids: list[str],
    *,
    experiment_name: Optional[str] = None,
    redis_conn: Optional[object] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Poll for completed single-POV verification verdicts.

    Non-blocking: returns immediately with completed results and
    remaining (still-pending) job IDs.

    Args:
        redis_host: Redis server hostname
        job_ids: List of verify job IDs to check

    Returns:
        Tuple of (completed_results, remaining_job_ids)
    """
    if not job_ids:
        return [], list(job_ids)

    if (
        get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER
        and experiment_name
        and all(job_id.startswith("verify:") for job_id in job_ids)
    ):
        try:
            store = _get_verify_claim_store(
                redis_host=redis_host,
                experiment_name=experiment_name,
                redis_conn=redis_conn,
            )
            completed, remaining = store.poll_results(job_ids)
            remaining_ids = set(remaining)
            completed_ids = [
                job_id for job_id in job_ids if job_id not in remaining_ids
            ]
            return (
                [
                    _normalize_dispatcher_result(job_id, result_dict)
                    for job_id, result_dict in zip(
                        completed_ids, completed, strict=False
                    )
                ],
                remaining,
            )
        except Exception as e:
            logger.warning(f"Failed to poll dispatcher POV verdicts: {e}")
            return [], list(job_ids)

    if not REDIS_AVAILABLE:
        return [], list(job_ids)

    completed: list[dict[str, Any]] = []
    remaining: list[str] = []

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        for job_id in job_ids:
            try:
                job = rq.job.Job.fetch(job_id, connection=redis_conn)
                status = job.get_status()
                if status == "finished" and job.result is not None:
                    completed.append(job.result)
                elif status == "finished":
                    completed.append(
                        _error_result_from_rq_job(
                            job,
                            default_error=(
                                "Verification finished without a result payload"
                            ),
                        )
                    )
                elif status == "failed":
                    exc_info = job.exc_info or "Unknown error"
                    completed.append(
                        _error_result_from_rq_job(job, default_error=str(exc_info))
                    )
                elif status in {"stopped", "canceled", "cancelled"}:
                    completed.append(
                        _error_result_from_rq_job(
                            job,
                            default_error=(
                                "Verification terminated with non-success "
                                f"job status: {status}"
                            ),
                        )
                    )
                else:
                    remaining.append(job_id)
            except Exception as e:
                logger.debug(f"Could not fetch verify job {job_id[:8]}: {e}")
                remaining.append(job_id)

    except Exception as e:
        logger.warning(f"Failed to poll single POV verdicts: {e}")
        remaining.extend(job_ids)

    return completed, remaining
