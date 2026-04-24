"""Evaluator process for distributed build and verify execution.

This module implements the evaluator process that:
1. Pre-builds benchmark variants at startup via _enqueue_pre_builds()
2. Listens on both build and verify Redis queues with fair runnable-job scheduling
3. Runs build jobs to create variant Docker images
4. Runs POV verification against built variants
5. Stores results as RQ job results
"""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from crsbench.distributed.common import (
    collect_validated_int_metadata,
    discover_registered_experiments,
    normalize_cpu_tag,
    normalize_redis_host,
    validate_optional_int_override,
)
from crsbench.distributed.evaluator_dispatcher import (
    EvaluatorDispatcher,
    build_evaluator_id,
    start_dispatcher_thread,
    start_presence_thread,
)
from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    build_scheduler_owner_key_for_ci_job,
)
from crsbench.distributed.queue import (
    REDIS_AVAILABLE,
    ROUTING_MODEL_DISPATCHER,
    create_redis_connection,
    get_evaluator_routing_model,
    resolve_evaluator_local_queue_names,
)
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.evaluation.results import TrialResult
from crsbench.utils.benchmark_utils import filter_benchmarks_by_mode
from crsbench.utils.cpu_pool import auto_cores_per_job, visible_cpu_count
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import threading

LEGACY_CI_ALIAS_EXPERIMENT = "__legacy_ci_alias__"
LEGACY_CI_BUILD_QUEUE = "crsbench_ci_build"
LEGACY_CI_VERIFY_QUEUE = "crsbench_ci_verify"
EVALUATOR_PROGRESS_LOG_EVERY_JOBS = 50


def _report_cloud_runtime_state(
    redis_host: str,
    *,
    state: str,
    detail: str | None = None,
    startup_evidence: str | None = None,
) -> None:
    """Best-effort readiness update for cloud-managed evaluator runtimes."""
    from crsbench.cloud.runtime import report_cloud_worker_state_from_env

    try:
        report_cloud_worker_state_from_env(
            redis_host=redis_host,
            state=state,
            detail=detail,
            startup_evidence=startup_evidence,
        )
    except Exception as exc:
        logger.warning(f"Failed to update cloud evaluator readiness state: {exc}")


def _is_duplicate_job_enqueue_error(exc: Exception) -> bool:
    """Best-effort duplicate enqueue detection across RQ versions."""
    msg = str(exc).lower()
    return "already exists" in msg or "job id" in msg and "exists" in msg


def _resolve_inc_image_runtime_settings(
    *,
    policy: object,
    registry: object,
    max_pull_bytes: object,
    pull_timeout_sec: object,
    local_prefix: object,
) -> tuple[str, str, Optional[int], int, str]:
    """Normalize inc-image runtime settings to safe primitive values."""
    resolved_policy = policy if isinstance(policy, str) and policy else "auto"
    if resolved_policy not in {"auto", "pull_only", "build_only"}:
        resolved_policy = "auto"

    resolved_registry = (
        registry
        if isinstance(registry, str) and registry
        else "ghcr.io/team-atlanta/crsbench"
    )

    resolved_max_pull_bytes: Optional[int] = None
    if isinstance(max_pull_bytes, int) and max_pull_bytes > 0:
        resolved_max_pull_bytes = max_pull_bytes

    resolved_pull_timeout = (
        pull_timeout_sec
        if isinstance(pull_timeout_sec, int) and pull_timeout_sec > 0
        else 300
    )
    resolved_local_prefix = (
        local_prefix if isinstance(local_prefix, str) and local_prefix else "crsbench"
    )
    return (
        resolved_policy,
        resolved_registry,
        resolved_max_pull_bytes,
        resolved_pull_timeout,
        resolved_local_prefix,
    )


def _enqueue_pre_builds(
    config,
    experiment_name: str,
    redis_host: str,
    *,
    inc_image_policy: str | None = None,
    inc_image_registry: str | None = None,
    inc_image_max_pull_bytes: int | None = None,
    inc_image_pull_timeout: int | None = None,
    local_image_prefix: str | None = None,
) -> int:
    """Enqueue build jobs for all experiment benchmarks at startup.

    Uses VariantPlanner to create BuildSingleVariantJob instances,
    then enqueues them to the evaluator's build queue so the supervisor
    processes them with proper CPU allocation (determined by queue name).

    Args:
        config: ExperimentConfig instance
        experiment_name: Experiment identifier for queue naming
        redis_host: Redis server hostname

    Returns:
        Number of build jobs enqueued
    """
    import rq

    from crsbench.distributed.ci_jobs import serialize_ci_job
    from crsbench.executor.variant_planner import VariantPlanner

    benchmarks_root = config.benchmarks_root

    benchmark_names = config.get_benchmark_list()

    # Filter benchmarks by mode early
    mode_str = config.mode.value
    if mode_str not in ("all", "auto"):
        original_count = len(benchmark_names)
        benchmark_names = filter_benchmarks_by_mode(
            benchmark_names, mode_str, benchmarks_root
        )
        if original_count != len(benchmark_names):
            logger.info(
                f"Filtered by mode={mode_str}: {len(benchmark_names)} of {original_count} benchmarks"
            )

    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    planner = VariantPlanner(
        oss_fuzz_path,
        source_mode=getattr(config, "source_mode", "pkgs"),
    )
    (
        inc_image_policy,
        inc_image_registry,
        inc_image_max_pull_bytes,
        inc_image_pull_timeout,
        local_image_prefix,
    ) = _resolve_inc_image_runtime_settings(
        policy=inc_image_policy
        if inc_image_policy is not None
        else getattr(config, "inc_image_policy", None),
        registry=inc_image_registry
        if inc_image_registry is not None
        else getattr(config, "inc_image_registry", None),
        max_pull_bytes=inc_image_max_pull_bytes
        if inc_image_max_pull_bytes is not None
        else getattr(config, "inc_image_max_pull_bytes", None),
        pull_timeout_sec=inc_image_pull_timeout
        if inc_image_pull_timeout is not None
        else getattr(config, "inc_image_pull_timeout_sec", None),
        local_prefix=local_image_prefix
        if local_image_prefix is not None
        else getattr(config, "project_image_prefix", None),
    )

    from crsbench.distributed.queue import create_redis_connection, resolve_queue_names

    redis_conn = create_redis_connection(redis_host)
    _trial_queue, build_queue_name, _verify_queue_name = resolve_queue_names(
        experiment_name
    )
    build_queue = rq.Queue(build_queue_name, connection=redis_conn)
    cpu_tag = config.resources.cpu_tag if config.resources else None
    base_job_meta = {"experiment_name": experiment_name}
    if cpu_tag:
        base_job_meta["cpu_tag"] = cpu_tag

    enqueued = 0
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Pre-build skip: {benchmark_path} not found")
            continue

        jobs = planner.plan_builds(
            benchmark_path,
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy=inc_image_policy,
            inc_image_registry=inc_image_registry,
            inc_image_max_pull_bytes=inc_image_max_pull_bytes,
            inc_image_pull_timeout=inc_image_pull_timeout,
            local_image_prefix=local_image_prefix,
        )

        for job in jobs:
            params = serialize_ci_job(job)
            job_meta = dict(base_job_meta)
            job_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_for_ci_job(
                job,
                experiment_name=experiment_name,
            )
            try:
                build_queue.enqueue(
                    "crsbench.distributed.build_jobs.execute_ci_build",
                    params,
                    job_timeout=3600,
                    result_ttl=-1,
                    job_id=job.job_id,
                    meta=job_meta,
                )
                enqueued += 1
            except Exception as e:
                if _is_duplicate_job_enqueue_error(e):
                    logger.debug(f"Pre-build job {job.job_id} already exists, skipping")
                    continue
                logger.exception("Failed to enqueue pre-build job {}", job.job_id)
                raise

    return enqueued


def run_evaluator_main(
    config,
    experiment_name: str,
    redis_host: str = "localhost",
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    build_jobs: Optional[int] = None,
    build_cores_per_job: Optional[int] = None,
    verify_cores_per_job: Optional[int] = None,
    verify_jobs: Optional[int] = None,
    idle_timeout: int = 0,
    worker_name: Optional[str] = None,
) -> int:
    """Main entry point for the evaluator process.

    Starts the dual-queue supervisor that processes both build and verify
    jobs. Runnable work is selected fairly across trial owners while
    build-before-verify ordering remains dependency-driven.

    Args:
        config: ExperimentConfig instance
        experiment_name: Experiment identifier for queue naming
        redis_host: Redis server hostname
        use_cpuset: Enable CPU affinity for jobs
        cores: CPU cores for evaluator pool (integer count or cpuset string)
        skip_cpus: CPUs to exclude from allocation (cpuset format)
        build_jobs: Max concurrent build jobs
        build_cores_per_job: CPUs per build job
        verify_cores_per_job: CPUs per verify job
        verify_jobs: Max concurrent verify jobs

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for evaluator execution")
        logger.error("Install with: pip install redis rq")
        return 1

    normalized_redis_host = normalize_redis_host(redis_host)
    if normalized_redis_host is None:
        logger.error(
            "Invalid redis host for evaluator. Set redis_host to a non-empty hostname."
        )
        return 1
    redis_host = normalized_redis_host

    from crsbench.distributed.queue import resolve_queue_names

    routing_model = get_evaluator_routing_model()
    resolved_worker_name = worker_name or f"evaluator-{experiment_name}"
    evaluator_id: str | None = None
    if routing_model == ROUTING_MODEL_DISPATCHER:
        evaluator_id = build_evaluator_id(resolved_worker_name)
        build_queue_name, verify_queue_name = resolve_evaluator_local_queue_names(
            experiment_name,
            evaluator_id,
        )
    else:
        _trial_queue, build_queue_name, verify_queue_name = resolve_queue_names(
            experiment_name
        )

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Evaluator")
    logger.info("=" * 60)
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Worker name: {resolved_worker_name}")
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Build jobs: {build_jobs or 1}")
    logger.info(f"CPU affinity: {'enabled' if use_cpuset else 'disabled'}")
    logger.info("Queues: build + verify (fair runnable-job scheduling)")
    logger.info("=" * 60)
    _report_cloud_runtime_state(
        redis_host,
        state="registering",
        detail="Preparing evaluator runtime for build and verify queues",
    )

    (
        resolved_policy,
        resolved_registry,
        resolved_max_pull_bytes,
        resolved_pull_timeout,
        resolved_local_prefix,
    ) = _resolve_inc_image_runtime_settings(
        policy=getattr(config, "inc_image_policy", None),
        registry=getattr(config, "inc_image_registry", None),
        max_pull_bytes=getattr(config, "inc_image_max_pull_bytes", None),
        pull_timeout_sec=getattr(config, "inc_image_pull_timeout_sec", None),
        local_prefix=getattr(config, "project_image_prefix", None),
    )

    # Create verification engine for lazy verify use
    from crsbench.evaluation.verification.pov.engine import VerificationEngine
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    oss_fuzz_path = Path(ensure_oss_fuzz_root())

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=config.per_pov_verify_timeout,
        source_mode=getattr(config, "source_mode", "pkgs"),
        inc_image_policy=resolved_policy,
        inc_image_registry=resolved_registry,
        inc_image_max_pull_bytes=resolved_max_pull_bytes,
        inc_image_pull_timeout=resolved_pull_timeout,
        local_image_prefix=resolved_local_prefix,
    )

    # Set engine and benchmarks root for lazy build loading
    from crsbench.distributed.evaluator_jobs import set_benchmarks_root, set_engine

    set_engine(engine)
    set_benchmarks_root(config.benchmarks_root)

    # Pre-build: enqueue variant builds for all experiment benchmarks
    if build_jobs is not None:
        if routing_model == ROUTING_MODEL_DISPATCHER:
            logger.info(
                "Dispatcher routing enabled; skipping legacy shared pre-build enqueue"
            )
        else:
            enqueued = _enqueue_pre_builds(
                config,
                experiment_name,
                redis_host,
                inc_image_policy=resolved_policy,
                inc_image_registry=resolved_registry,
                inc_image_max_pull_bytes=resolved_max_pull_bytes,
                inc_image_pull_timeout=resolved_pull_timeout,
                local_image_prefix=resolved_local_prefix,
            )
            logger.info(f"Pre-build: enqueued {enqueued} build jobs")

    # Start dual-queue supervisor
    from crsbench.distributed.ci_supervisor import run_ci_supervisor

    logger.info("Starting dual-queue supervisor (build + verify)...")
    presence_loop: tuple[threading.Event, threading.Thread] | None = None
    dispatcher_loop: tuple[threading.Event, threading.Thread] | None = None
    if routing_model == ROUTING_MODEL_DISPATCHER and evaluator_id is not None:
        presence_loop = start_presence_thread(
            redis_host=redis_host,
            experiment_name=experiment_name,
            evaluator_id=evaluator_id,
            worker_name=resolved_worker_name,
        )
        dispatcher = EvaluatorDispatcher(
            redis_conn=create_redis_connection(redis_host),
            experiment_name=experiment_name,
            evaluator_id=evaluator_id,
        )
        dispatcher_loop = start_dispatcher_thread(dispatcher)
    try:
        _report_cloud_runtime_state(
            redis_host,
            state="ready",
            detail="Evaluator supervisor managing build and verify queues",
        )
        return run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name=build_queue_name,
            verify_queue_name=verify_queue_name,
            worker_name=resolved_worker_name,
            build_jobs=build_jobs or 1,
            build_cores_per_job=build_cores_per_job,
            verify_cores_per_job=verify_cores_per_job,
            verify_jobs=verify_jobs or (build_jobs or 1),
            job_runner=_evaluator_job_runner,
            use_cpuset=use_cpuset,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            cpu_tag=cpu_tag,
            idle_timeout=idle_timeout,
            progress_log_every_jobs=EVALUATOR_PROGRESS_LOG_EVERY_JOBS,
        )
    finally:
        if dispatcher_loop is not None:
            stop_event, thread = dispatcher_loop
            stop_event.set()
            thread.join(timeout=1)
        if presence_loop is not None:
            stop_event, thread = presence_loop
            stop_event.set()
            thread.join(timeout=1)


def _evaluator_job_runner(
    redis_host: str,
    child_name: str,
    job_id: str,
) -> None:
    """Adapter for ci_supervisor: delegates to _run_single_job."""
    _run_single_job(
        redis_host, job_id, child_name=child_name, execution_role="evaluator"
    )


def _run_single_job(
    redis_host: str,
    job_id: str,
    child_name: str = "",
    execution_role: str = "worker",
) -> None:
    """Execute a single job (build or verify) in a child process.

    Generic job runner that fetches an RQ job and calls perform().
    Works for both build and verify jobs.

    Args:
        redis_host: Redis server hostname
        job_id: RQ job ID to execute
        child_name: Worker name assigned by the supervisor (for log context)
    """
    import rq
    import rq.job
    import rq.utils
    from rq.executions import Execution
    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry, FinishedJobRegistry
    from rq.results import Result

    # Reconfigure logging in subprocess
    configure_logger(
        level=os.environ.get("CRSBENCH_LOG_LEVEL", "INFO").upper(), sink=sys.stdout
    )

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        job = rq.job.Job.fetch(job_id, connection=redis_conn)
        queue = rq.Queue(job.origin, connection=redis_conn)

        finished_registry = FinishedJobRegistry(queue=queue)
        failed_registry = FailedJobRegistry(queue=queue)

        label = f"[{child_name}] " if child_name else ""
        role = execution_role.capitalize()
        logger.info(f"{label}{role} executing job {job_id} from queue {job.origin}")

        # Propagate per-job cpu_tag to nested queue producers (async verify /
        # patch queue jobs) so downstream build/verify jobs keep the same tag.
        job_cpu_tag = job.meta.get("cpu_tag")
        if job_cpu_tag:
            os.environ["CRSBENCH_JOB_CPU_TAG"] = str(job_cpu_tag)
        else:
            os.environ.pop("CRSBENCH_JOB_CPU_TAG", None)

        # Create execution and mark as STARTED
        execution = None
        execution_owner = child_name or execution_role
        claim_lease_key = f"{queue.intermediate_queue.key}:fair-claim:{job.id}"
        os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = execution_owner
        with redis_conn.pipeline() as pipeline:
            job.prepare_for_execution(execution_owner, pipeline=pipeline)
            pipeline.lrem(queue.intermediate_queue.key, 1, job.id)
            pipeline.delete(claim_lease_key)
            execution = Execution.create(job, ttl=-1, pipeline=pipeline)
            pipeline.execute()

        try:
            result = job.perform()

            # Check if this is a TrialResult with success=False
            if isinstance(result, TrialResult) and not result.success:
                # Treat as failed job
                with redis_conn.pipeline() as pipeline:
                    job._status = JobStatus.FAILED
                    job.ended_at = rq.utils.now()
                    job._result = result
                    job.save_meta()
                    pipeline.hset(
                        job.key,
                        mapping={
                            "status": JobStatus.FAILED,
                            "ended_at": rq.utils.utcformat(job.ended_at),
                        },
                    )
                    exc_string = f"Trial failed: {result.error_type}: {result.error}"
                    Result.create(
                        job,
                        Result.Type.FAILED,
                        ttl=-1,
                        return_value=result,
                        exc_string=exc_string,
                        pipeline=pipeline,
                    )
                    if execution:
                        execution.delete(job, pipeline=pipeline)
                    failed_registry.add(
                        job, ttl=-1, exc_string=exc_string, pipeline=pipeline
                    )
                    pipeline.execute()

                logger.warning(f"Job {job_id} failed: {result.error}")
            else:
                # Mark as FINISHED and persist result to Redis
                with redis_conn.pipeline() as pipeline:
                    job._status = JobStatus.FINISHED
                    job.ended_at = rq.utils.now()
                    job._result = result
                    job.save_meta()
                    pipeline.hset(
                        job.key,
                        mapping={
                            "status": JobStatus.FINISHED,
                            "ended_at": rq.utils.utcformat(job.ended_at),
                        },
                    )
                    Result.create(
                        job,
                        Result.Type.SUCCESSFUL,
                        ttl=-1,
                        return_value=result,
                        pipeline=pipeline,
                    )
                    if execution:
                        execution.delete(job, pipeline=pipeline)
                    finished_registry.add(job, ttl=-1, pipeline=pipeline)
                    pipeline.execute()

                if isinstance(result, dict) and (
                    result.get("success") is False
                    or result.get("status")
                    in {"error", "build_failed", "pov_still_triggers", "test_failed"}
                ):
                    logger.warning(
                        "Job "
                        f"{job_id} completed with non-success result "
                        f"(status={result.get('status')}, "
                        f"error={result.get('error', '')})"
                    )
                else:
                    logger.info(f"Job {job_id} completed successfully")

        except Exception as e:
            import traceback

            exc_string = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )

            with redis_conn.pipeline() as pipeline:
                job._status = JobStatus.FAILED
                job.ended_at = rq.utils.now()
                pipeline.hset(
                    job.key,
                    mapping={
                        "status": JobStatus.FAILED,
                        "ended_at": rq.utils.utcformat(job.ended_at),
                    },
                )
                if execution:
                    execution.delete(job, pipeline=pipeline)
                failed_registry.add(
                    job, ttl=-1, exc_string=exc_string, pipeline=pipeline
                )
                pipeline.execute()

            logger.exception("Job {} failed", job_id)
            raise

    except Exception:
        logger.exception("Evaluator worker error")
        raise


def _enqueue_pre_builds_from_registration(
    registration,
    redis_host: str,
    benchmarks_root,
) -> int:
    """Enqueue build jobs using registration metadata instead of full config.

    Similar to ``_enqueue_pre_builds()`` but derives parameters from a
    ``RuntimeRegistration`` rather than an ``ExperimentConfig``.

    Args:
        registration: RuntimeRegistration instance from the registry.
        redis_host: Redis server hostname.
        benchmarks_root: Path to benchmarks root directory.

    Returns:
        Number of build jobs enqueued.
    """
    from pathlib import Path

    import rq

    from crsbench.distributed.ci_jobs import serialize_ci_job
    from crsbench.distributed.queue import create_redis_connection
    from crsbench.executor.variant_planner import VariantPlanner
    from crsbench.utils.benchmark_utils import filter_benchmarks_by_mode

    # Registry payloads carry path fields as strings; normalize once so
    # downstream path arithmetic and mode filtering are always Path-safe.
    benchmarks_root = Path(benchmarks_root)
    benchmark_names = list(registration.benchmarks)

    # Filter by mode if specified
    modes = registration.modes
    if modes and modes[0] not in ("all", "auto"):
        original_count = len(benchmark_names)
        benchmark_names = filter_benchmarks_by_mode(
            benchmark_names, modes[0], benchmarks_root
        )
        if original_count != len(benchmark_names):
            logger.info(
                f"Filtered by mode={modes[0]}: {len(benchmark_names)} of {original_count} benchmarks"
            )

    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    planner = VariantPlanner(oss_fuzz_path, source_mode=registration.source_mode)

    redis_conn = create_redis_connection(redis_host)
    build_queue = rq.Queue(registration.build_queue, connection=redis_conn)
    base_job_meta = {"experiment_name": registration.experiment}
    if registration.cpu_tag:
        base_job_meta["cpu_tag"] = registration.cpu_tag

    enqueued = 0
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Pre-build skip: {benchmark_path} not found")
            continue

        jobs = planner.plan_builds(
            benchmark_path,
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy=registration.inc_image_policy,
            inc_image_registry=registration.inc_image_registry,
            inc_image_max_pull_bytes=registration.inc_image_max_pull_bytes,
            inc_image_pull_timeout=registration.inc_image_pull_timeout_sec,
            local_image_prefix=registration.local_image_prefix,
        )

        for job in jobs:
            params = serialize_ci_job(job)
            job_meta = dict(base_job_meta)
            job_meta[SCHEDULER_OWNER_KEY_META] = build_scheduler_owner_key_for_ci_job(
                job,
                experiment_name=registration.experiment,
            )
            try:
                build_queue.enqueue(
                    "crsbench.distributed.build_jobs.execute_ci_build",
                    params,
                    job_timeout=registration.build_timeout,
                    result_ttl=-1,
                    job_id=job.job_id,
                    meta=job_meta,
                )
                enqueued += 1
            except Exception as e:
                if _is_duplicate_job_enqueue_error(e):
                    logger.debug(f"Pre-build job {job.job_id} already exists, skipping")
                    continue
                logger.exception("Failed to enqueue pre-build job {}", job.job_id)
                raise

    return enqueued


def run_evaluator_configless(
    redis_host: str = "localhost",
    worker_name: str = "configless-evaluator",
    build_jobs: Optional[int] = None,
    build_cores_per_job: Optional[int] = None,
    verify_cores_per_job: Optional[int] = None,
    verify_jobs: Optional[int] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    idle_timeout: Optional[int] = None,
    benchmarks_root: Optional[str] = None,
    legacy_ci_alias: bool = False,
) -> int:
    """Run evaluator in configless mode — discover experiments from Redis registry.

    Connects to the Redis registry, discovers all registered experiments, sets
    up a verification engine from registration metadata, then starts a
    multi-queue supervisor across all discovered build/verify queues.

    Limitation: All experiments must share the same ``benchmarks_root`` on the
    evaluator machine. OSS-Fuzz root is resolved from managed runtime bootstrap.

    Args:
        redis_host: Redis server hostname.
        worker_name: Worker name for identification.
        build_jobs: Max concurrent build jobs.
        build_cores_per_job: CPUs per build job.
        verify_cores_per_job: CPUs per verify job.
        verify_jobs: Max concurrent verify jobs (default: build_jobs).
        use_cpuset: Enable CPU affinity.
        cores: CPU cores for pool.
        skip_cpus: CPUs to exclude.
        idle_timeout: Idle timeout in seconds after backlog drains and both
            build/verify queues remain idle.
        benchmarks_root: Override benchmarks root (default: from first registration).

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for evaluator execution")
        return 1
    normalized_redis_host = normalize_redis_host(redis_host)
    if normalized_redis_host is None:
        logger.error(
            "Configless evaluator requires a Redis host. "
            "Set CRSBENCH_REDIS_HOST to a non-empty hostname."
        )
        return 1
    redis_host = normalized_redis_host
    if get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER:
        logger.error(
            "Dispatcher routing currently supports only --experiment-config mode"
        )
        return 1

    logger.info("=" * 60)
    logger.info("CRSBench Evaluator — Configless Mode")
    logger.info("=" * 60)
    logger.info(f"Redis host: {redis_host}")
    _report_cloud_runtime_state(
        redis_host,
        state="registering",
        detail="Preparing evaluator runtime for discovered build and verify queues",
    )

    if legacy_ci_alias:
        logger.info(
            "--ci compatibility alias enabled; using unified evaluator "
            "supervisor path with legacy CI queues"
        )
        experiments = {
            LEGACY_CI_ALIAS_EXPERIMENT: RuntimeRegistration(
                experiment=LEGACY_CI_ALIAS_EXPERIMENT,
                trial_queue="",
                build_queue=LEGACY_CI_BUILD_QUEUE,
                verify_queue=LEGACY_CI_VERIFY_QUEUE,
                benchmarks_root=benchmarks_root or "benchmarks",
            )
        }
    else:
        logger.info("Discovering experiments from registry...")
        _redis_conn, experiments = discover_registered_experiments(redis_host)

    logger.info("=" * 60)

    logger.info(f"Discovered {len(experiments)} experiment(s)")

    try:
        build_jobs = validate_optional_int_override(
            value=build_jobs,
            field_name="evaluator.build_jobs",
            minimum=1,
        )
        build_cores_per_job = validate_optional_int_override(
            value=build_cores_per_job,
            field_name="evaluator.build_cores_per_job",
            minimum=1,
        )
        verify_jobs = validate_optional_int_override(
            value=verify_jobs,
            field_name="evaluator.verify_jobs",
            minimum=1,
        )
        verify_cores_per_job = validate_optional_int_override(
            value=verify_cores_per_job,
            field_name="evaluator.verify_cores_per_job",
            minimum=1,
        )
        idle_timeout = validate_optional_int_override(
            value=idle_timeout,
            field_name="evaluator.idle_timeout",
            minimum=0,
        )
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    # Validate shared paths — all registrations must agree on benchmarks_root
    # since a single evaluator serves all experiments.
    first_reg = next(iter(experiments.values()))
    first_inc_settings = _resolve_inc_image_runtime_settings(
        policy=first_reg.inc_image_policy,
        registry=first_reg.inc_image_registry,
        max_pull_bytes=first_reg.inc_image_max_pull_bytes,
        pull_timeout_sec=first_reg.inc_image_pull_timeout_sec,
        local_prefix=first_reg.local_image_prefix,
    )
    for name, reg in experiments.items():
        if reg.benchmarks_root != first_reg.benchmarks_root:
            logger.error(
                f"Experiment '{name}' has benchmarks_root='{reg.benchmarks_root}' "
                f"but '{first_reg.experiment}' has '{first_reg.benchmarks_root}'. "
                "All experiments on a configless evaluator must share the same paths. "
                "Use separate evaluator processes or --experiment-config mode."
            )
            return 1
        reg_inc_settings = _resolve_inc_image_runtime_settings(
            policy=reg.inc_image_policy,
            registry=reg.inc_image_registry,
            max_pull_bytes=reg.inc_image_max_pull_bytes,
            pull_timeout_sec=reg.inc_image_pull_timeout_sec,
            local_prefix=reg.local_image_prefix,
        )
        if reg_inc_settings != first_inc_settings:
            logger.error(
                f"Experiment '{name}' has inc-image runtime settings {reg_inc_settings} "
                f"but '{first_reg.experiment}' has {first_inc_settings}. "
                "All experiments on a configless evaluator must share inc-image "
                "policy/registry/pull limits/local prefix. "
                "Use separate evaluator processes or --experiment-config mode."
            )
            return 1
    effective_benchmarks_root = benchmarks_root or first_reg.benchmarks_root
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    per_pov_verify_timeout = max(
        reg.per_pov_verify_timeout for reg in experiments.values()
    )

    (
        resolved_policy,
        resolved_registry,
        resolved_max_pull_bytes,
        resolved_pull_timeout,
        resolved_local_prefix,
    ) = _resolve_inc_image_runtime_settings(
        policy=first_reg.inc_image_policy,
        registry=first_reg.inc_image_registry,
        max_pull_bytes=first_reg.inc_image_max_pull_bytes,
        pull_timeout_sec=first_reg.inc_image_pull_timeout_sec,
        local_prefix=first_reg.local_image_prefix,
    )

    # Set up verification engine
    from crsbench.evaluation.verification.pov.engine import VerificationEngine

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=per_pov_verify_timeout,
        source_mode=first_reg.source_mode,
        inc_image_policy=resolved_policy,
        inc_image_registry=resolved_registry,
        inc_image_max_pull_bytes=resolved_max_pull_bytes,
        inc_image_pull_timeout=resolved_pull_timeout,
        local_image_prefix=resolved_local_prefix,
    )

    from crsbench.distributed.evaluator_jobs import set_benchmarks_root, set_engine

    set_engine(engine)
    set_benchmarks_root(Path(effective_benchmarks_root))

    # Use stable experiment ordering so metadata precedence is deterministic.
    ordered_regs = [experiments[name] for name in sorted(experiments)]
    source_modes = sorted({reg.source_mode for reg in ordered_regs})
    if len(source_modes) > 1:
        logger.error(
            "Configless evaluator cannot merge experiments with different "
            "source_mode values: {}",
            ", ".join(source_modes),
        )
        return 1

    # Collect all build and verify queue names
    build_queue_names = sorted({reg.build_queue for reg in ordered_regs})
    verify_queue_names = sorted({reg.verify_queue for reg in ordered_regs})

    # Resolve evaluator resources (CLI > metadata > defaults)
    try:
        meta_build_jobs = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="evaluator_build_jobs",
            field_name="evaluator.build_jobs",
            minimum=1,
        )
        meta_build_cores = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="evaluator_build_cores_per_job",
            field_name="evaluator.build_cores_per_job",
            minimum=1,
        )
        meta_verify_jobs = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="evaluator_verify_jobs",
            field_name="evaluator.verify_jobs",
            minimum=1,
        )
        meta_verify_cores = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="evaluator_verify_cores_per_job",
            field_name="evaluator.verify_cores_per_job",
            minimum=1,
        )
        meta_idle_timeout = collect_validated_int_metadata(
            registrations=ordered_regs,
            attr_name="evaluator_idle_timeout",
            field_name="evaluator.idle_timeout",
            minimum=0,
        )
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1
    meta_cpu_tags_ordered: list[str] = []
    for reg in ordered_regs:
        candidate_tag = normalize_cpu_tag(reg.evaluator_cpu_tag) or normalize_cpu_tag(
            reg.cpu_tag
        )
        if candidate_tag is not None:
            meta_cpu_tags_ordered.append(candidate_tag)

    resolved_build_jobs = (
        build_jobs
        if build_jobs is not None
        else (max(meta_build_jobs) if meta_build_jobs else 1)
    )
    resolved_build_cores_per_job = (
        build_cores_per_job
        if build_cores_per_job is not None
        else (max(meta_build_cores) if meta_build_cores else None)
    )
    resolved_verify_cores_per_job = (
        verify_cores_per_job
        if verify_cores_per_job is not None
        else (max(meta_verify_cores) if meta_verify_cores else None)
    )
    effective_build_cores_per_job = (
        resolved_build_cores_per_job
        if resolved_build_cores_per_job is not None
        else (
            auto_cores_per_job(
                resolved_build_jobs,
                cores=cores,
                skip_cpus=skip_cpus,
            )
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )
    effective_verify_cores_per_job = (
        resolved_verify_cores_per_job
        if resolved_verify_cores_per_job is not None
        else (
            auto_cores_per_job(
                (
                    verify_jobs
                    if verify_jobs is not None
                    else (
                        max(meta_verify_jobs)
                        if meta_verify_jobs
                        else resolved_build_jobs
                    )
                ),
                cores=cores,
                skip_cpus=skip_cpus,
            )
            if use_cpuset
            else visible_cpu_count(cores=cores, skip_cpus=skip_cpus)
        )
    )
    resolved_verify_jobs = (
        verify_jobs
        if verify_jobs is not None
        else (
            max(meta_verify_jobs)
            if meta_verify_jobs
            else max(
                1,
                (resolved_build_jobs * effective_build_cores_per_job)
                // effective_verify_cores_per_job,
            )
        )
    )
    resolved_idle_timeout = (
        idle_timeout
        if idle_timeout is not None
        else (max(meta_idle_timeout) if meta_idle_timeout else 0)
    )

    resolved_cpuset = cores
    resolved_skip_cpuset = skip_cpus
    if cpu_tag is not None:
        resolved_cpu_tag = normalize_cpu_tag(cpu_tag)
    else:
        distinct_cpu_tags = sorted(set(meta_cpu_tags_ordered))
        if len(distinct_cpu_tags) > 1:
            logger.error(
                "Conflicting evaluator.cpu_tag/resources.cpu_tag metadata across "
                "experiments. Set --cpu-tag explicitly to run this evaluator."
            )
            return 1
        resolved_cpu_tag = distinct_cpu_tags[0] if distinct_cpu_tags else None

    logger.info(f"Build queues: {build_queue_names}")
    logger.info(f"Verify queues: {verify_queue_names}")

    logger.info(
        "Configless evaluator skips startup pre-build. "
        "Builds are consumed lazily from build queues."
    )

    # Start multi-queue supervisor
    from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

    logger.info(
        "Evaluator resource profile (CLI > metadata > runtime envelope): "
        f"build_jobs={resolved_build_jobs}, "
        f"build_cores_per_job={resolved_build_cores_per_job if resolved_build_cores_per_job is not None else 'auto'}, "
        f"effective_build_cores_per_job={effective_build_cores_per_job}, "
        f"verify_jobs={resolved_verify_jobs}, "
        f"verify_cores_per_job={resolved_verify_cores_per_job if resolved_verify_cores_per_job is not None else 'auto'}, "
        f"effective_verify_cores_per_job={effective_verify_cores_per_job}, "
        f"cpuset={resolved_cpuset}, skip_cpuset={resolved_skip_cpuset} (CLI-owned), "
        f"cpu_tag={resolved_cpu_tag}, idle_timeout={resolved_idle_timeout}"
    )

    logger.info("Starting multi-queue supervisor (build + verify)...")
    _report_cloud_runtime_state(
        redis_host,
        state="ready",
        detail="Evaluator supervisor managing build and verify queues",
    )

    queue_refresher = None
    if not legacy_ci_alias:
        warned_incompatible: set[tuple[str, str]] = set()

        def _warn_incompatible_once(experiment_name: str, reason: str) -> None:
            key = (experiment_name, reason)
            if key in warned_incompatible:
                return
            warned_incompatible.add(key)
            logger.warning(
                "Skipping experiment due to incompatible evaluator resource profile: "
                f"{experiment_name} ({reason})"
            )

        def _is_refresh_compatible(
            experiment_name: str, reg: RuntimeRegistration
        ) -> bool:
            if reg.benchmarks_root != first_reg.benchmarks_root:
                _warn_incompatible_once(
                    experiment_name,
                    "benchmarks_root mismatch "
                    f"(expected={first_reg.benchmarks_root}, got={reg.benchmarks_root})",
                )
                return False

            reg_inc_settings = _resolve_inc_image_runtime_settings(
                policy=reg.inc_image_policy,
                registry=reg.inc_image_registry,
                max_pull_bytes=reg.inc_image_max_pull_bytes,
                pull_timeout_sec=reg.inc_image_pull_timeout_sec,
                local_prefix=reg.local_image_prefix,
            )
            if reg_inc_settings != first_inc_settings:
                _warn_incompatible_once(
                    experiment_name,
                    "inc-image settings mismatch "
                    f"(expected={first_inc_settings}, got={reg_inc_settings})",
                )
                return False

            try:
                reg_build_jobs_values = collect_validated_int_metadata(
                    registrations=[reg],
                    attr_name="evaluator_build_jobs",
                    field_name="evaluator.build_jobs",
                    minimum=1,
                )
                reg_build_cores_values = collect_validated_int_metadata(
                    registrations=[reg],
                    attr_name="evaluator_build_cores_per_job",
                    field_name="evaluator.build_cores_per_job",
                    minimum=1,
                )
                reg_verify_jobs_values = collect_validated_int_metadata(
                    registrations=[reg],
                    attr_name="evaluator_verify_jobs",
                    field_name="evaluator.verify_jobs",
                    minimum=1,
                )
                reg_verify_cores_values = collect_validated_int_metadata(
                    registrations=[reg],
                    attr_name="evaluator_verify_cores_per_job",
                    field_name="evaluator.verify_cores_per_job",
                    minimum=1,
                )
                reg_idle_timeout_values = collect_validated_int_metadata(
                    registrations=[reg],
                    attr_name="evaluator_idle_timeout",
                    field_name="evaluator.idle_timeout",
                    minimum=0,
                )
            except RuntimeError as exc:
                _warn_incompatible_once(experiment_name, str(exc))
                return False

            required_build_jobs = (
                reg_build_jobs_values[0] if reg_build_jobs_values else 1
            )
            required_build_cores = (
                reg_build_cores_values[0] if reg_build_cores_values else None
            )
            required_verify_cores = (
                reg_verify_cores_values[0] if reg_verify_cores_values else None
            )
            required_verify_jobs = (
                reg_verify_jobs_values[0]
                if reg_verify_jobs_values
                else resolved_verify_jobs
            )
            required_idle_timeout = (
                reg_idle_timeout_values[0] if reg_idle_timeout_values else 0
            )

            if required_build_jobs > resolved_build_jobs:
                _warn_incompatible_once(
                    experiment_name,
                    "requires evaluator.build_jobs="
                    f"{required_build_jobs} but evaluator runs with build_jobs={resolved_build_jobs}",
                )
                return False
            if (
                required_build_cores is not None
                and required_build_cores > effective_build_cores_per_job
            ):
                _warn_incompatible_once(
                    experiment_name,
                    "requires evaluator.build_cores_per_job="
                    f"{required_build_cores} but evaluator runs with build_cores_per_job={effective_build_cores_per_job}",
                )
                return False
            if required_verify_jobs > resolved_verify_jobs:
                _warn_incompatible_once(
                    experiment_name,
                    "requires evaluator.verify_jobs="
                    f"{required_verify_jobs} but evaluator runs with verify_jobs={resolved_verify_jobs}",
                )
                return False
            if (
                required_verify_cores is not None
                and required_verify_cores > effective_verify_cores_per_job
            ):
                _warn_incompatible_once(
                    experiment_name,
                    "requires evaluator.verify_cores_per_job="
                    f"{required_verify_cores} but evaluator runs with verify_cores_per_job={effective_verify_cores_per_job}",
                )
                return False
            if required_idle_timeout > resolved_idle_timeout:
                _warn_incompatible_once(
                    experiment_name,
                    "requires evaluator.idle_timeout="
                    f"{required_idle_timeout} but evaluator runs with idle_timeout={resolved_idle_timeout}",
                )
                return False

            reg_cpu_tag = normalize_cpu_tag(reg.evaluator_cpu_tag) or normalize_cpu_tag(
                reg.cpu_tag
            )
            if reg_cpu_tag is not None and reg_cpu_tag != resolved_cpu_tag:
                _warn_incompatible_once(
                    experiment_name,
                    f"requires cpu_tag={reg_cpu_tag!r} but evaluator runs with cpu_tag={resolved_cpu_tag!r}",
                )
                return False

            return True

        def _refresh_evaluator_queues(redis_conn):
            from crsbench.distributed.registry import RegistryClient

            registry = RegistryClient(redis_conn)
            refreshed = registry.list_experiments()
            if not refreshed:
                return [], []

            compatible = []
            for name in sorted(refreshed):
                reg = refreshed[name]
                if not _is_refresh_compatible(name, reg):
                    continue
                compatible.append(reg)

            return (
                sorted({reg.build_queue for reg in compatible}),
                sorted({reg.verify_queue for reg in compatible}),
            )

        queue_refresher = _refresh_evaluator_queues

    return run_multi_queue_supervisor(
        redis_host=redis_host,
        build_queue_names=build_queue_names,
        verify_queue_names=verify_queue_names,
        worker_name=worker_name,
        build_jobs=resolved_build_jobs,
        build_cores_per_job=resolved_build_cores_per_job,
        verify_cores_per_job=resolved_verify_cores_per_job,
        verify_jobs=resolved_verify_jobs,
        job_runner=_evaluator_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=resolved_cpuset,
        skip_cpus=resolved_skip_cpuset,
        idle_timeout=resolved_idle_timeout,
        queue_refresher=queue_refresher,
        cpu_tag=resolved_cpu_tag,
        progress_log_every_jobs=EVALUATOR_PROGRESS_LOG_EVERY_JOBS,
    )


def run_evaluator_ci_mode(
    redis_host: str = "localhost",
    worker_name: str = "ci-evaluator",
    build_jobs: Optional[int] = None,
    build_cores_per_job: Optional[int] = None,
    verify_cores_per_job: Optional[int] = None,
    verify_jobs: Optional[int] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    cpu_tag: Optional[str] = None,
    idle_timeout: Optional[int] = None,
) -> int:
    """Run evaluator in --ci compatibility mode.

    Backward-compatible entrypoint that now reuses the unified configless
    evaluator path with legacy CI queue names.

    Args:
        redis_host: Redis server hostname.
        worker_name: Worker name for identification.
        build_jobs: Max concurrent build jobs.
        build_cores_per_job: CPUs per build job.
        verify_cores_per_job: CPUs per verify job.
        verify_jobs: Max concurrent verify jobs (default: build_jobs).
        use_cpuset: Enable CPU affinity.
        cores: CPU cores for pool (integer count or cpuset string).
        skip_cpus: CPUs to exclude (cpuset format).

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER:
        logger.error(
            "Dispatcher routing currently supports only --experiment-config mode"
        )
        return 1
    return run_evaluator_configless(
        redis_host=redis_host,
        worker_name=worker_name,
        build_jobs=build_jobs,
        build_cores_per_job=build_cores_per_job,
        verify_jobs=verify_jobs,
        verify_cores_per_job=verify_cores_per_job,
        use_cpuset=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
        cpu_tag=cpu_tag,
        idle_timeout=idle_timeout,
        legacy_ci_alias=True,
    )
