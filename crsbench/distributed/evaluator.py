"""Evaluator process for distributed build and verify execution.

This module implements the evaluator process that:
1. Pre-builds benchmark variants at startup via _enqueue_pre_builds()
2. Listens on both build and verify Redis queues (build has priority)
3. Runs build jobs to create variant Docker images
4. Runs POV verification against built variants
5. Stores results as RQ job results
"""

import os
import sys
from typing import Optional

from crsbench.distributed.common import (
    collect_validated_int_metadata,
    discover_registered_experiments,
    normalize_cpu_tag,
    normalize_redis_host,
    resolve_cli_or_first_metadata,
    validate_optional_int_override,
)
from crsbench.distributed.queue import REDIS_AVAILABLE
from crsbench.evaluation.results import TrialResult
from crsbench.utils.benchmark_utils import filter_benchmarks_by_mode
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def _enqueue_pre_builds(
    config,
    experiment_name: str,
    redis_host: str,
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
    if mode_str != "all":
        original_count = len(benchmark_names)
        benchmark_names = filter_benchmarks_by_mode(
            benchmark_names, mode_str, benchmarks_root
        )
        if original_count != len(benchmark_names):
            logger.info(
                f"Filtered by mode={mode_str}: {len(benchmark_names)} of {original_count} benchmarks"
            )

    oss_fuzz_path = config.oss_fuzz_path
    planner = VariantPlanner(oss_fuzz_path, source_mode="pkgs")

    from crsbench.distributed.queue import create_redis_connection, resolve_queue_names

    redis_conn = create_redis_connection(redis_host)
    _trial_queue, build_queue_name, _verify_queue_name = resolve_queue_names(
        experiment_name
    )
    build_queue = rq.Queue(build_queue_name, connection=redis_conn)
    cpu_tag = config.resources.cpu_tag if config.resources else None
    job_meta = {"cpu_tag": cpu_tag} if cpu_tag else {}

    enqueued = 0
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Pre-build skip: {benchmark_path} not found")
            continue

        jobs = planner.plan_builds(
            benchmark_path,
            use_inc_build=False,
            skip_if_cached=True,
        )

        for job in jobs:
            params = serialize_ci_job(job)
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
            except Exception:
                # Job with same ID already exists (dedup)
                logger.debug(f"Pre-build job {job.job_id} already exists, skipping")

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
) -> int:
    """Main entry point for the evaluator process.

    Starts the dual-queue supervisor that processes both build and verify
    jobs. Build queue has priority over verify queue.

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

    from crsbench.distributed.queue import resolve_queue_names

    _trial_queue, build_queue_name, verify_queue_name = resolve_queue_names(
        experiment_name
    )

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Evaluator")
    logger.info("=" * 60)
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Build jobs: {build_jobs or 1}")
    logger.info(f"CPU affinity: {'enabled' if use_cpuset else 'disabled'}")
    logger.info("Queues: build (priority) + verify")
    logger.info("=" * 60)

    # Create verification engine for lazy verify use
    from crsbench.evaluation.verification.pov.engine import VerificationEngine

    oss_fuzz_path = config.oss_fuzz_path

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=config.per_pov_verify_timeout,
    )

    # Set engine and benchmarks root for lazy build loading
    from crsbench.distributed.evaluator_jobs import set_benchmarks_root, set_engine

    set_engine(engine)
    set_benchmarks_root(config.benchmarks_root)

    # Pre-build: enqueue variant builds for all experiment benchmarks
    if build_jobs is not None:
        enqueued = _enqueue_pre_builds(
            config,
            experiment_name,
            redis_host,
        )
        logger.info(f"Pre-build: enqueued {enqueued} build jobs")

    # Start dual-queue supervisor
    from crsbench.distributed.ci_supervisor import run_ci_supervisor

    logger.info("Starting dual-queue supervisor (build + verify)...")
    return run_ci_supervisor(
        redis_host=redis_host,
        build_queue_name=build_queue_name,
        verify_queue_name=verify_queue_name,
        worker_name=f"evaluator-{experiment_name}",
        build_jobs=build_jobs or 1,
        build_cores_per_job=build_cores_per_job or 4,
        verify_cores_per_job=verify_cores_per_job or 4,
        verify_jobs=verify_jobs or (build_jobs or 1),
        job_runner=_evaluator_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
        cpu_tag=cpu_tag,
        idle_timeout=idle_timeout,
    )


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
        os.environ["CRSBENCH_WORKER_DISPLAY_NAME"] = execution_owner
        with redis_conn.pipeline() as pipeline:
            job.prepare_for_execution(execution_owner, pipeline=pipeline)
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

            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            raise

    except Exception as e:
        logger.error(f"Evaluator worker error: {e}", exc_info=True)
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
    if modes and modes[0] != "all":
        original_count = len(benchmark_names)
        benchmark_names = filter_benchmarks_by_mode(
            benchmark_names, modes[0], benchmarks_root
        )
        if original_count != len(benchmark_names):
            logger.info(
                f"Filtered by mode={modes[0]}: {len(benchmark_names)} of {original_count} benchmarks"
            )

    oss_fuzz_path = registration.oss_fuzz_path
    planner = VariantPlanner(oss_fuzz_path, source_mode=registration.source_mode)

    redis_conn = create_redis_connection(redis_host)
    build_queue = rq.Queue(registration.build_queue, connection=redis_conn)
    job_meta = {"cpu_tag": registration.cpu_tag} if registration.cpu_tag else {}

    enqueued = 0
    for name in benchmark_names:
        benchmark_path = benchmarks_root / name
        if not benchmark_path.exists():
            logger.warning(f"Pre-build skip: {benchmark_path} not found")
            continue

        jobs = planner.plan_builds(
            benchmark_path,
            use_inc_build=False,
            skip_if_cached=True,
        )

        for job in jobs:
            params = serialize_ci_job(job)
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
            except Exception:
                logger.debug(f"Pre-build job {job.job_id} already exists, skipping")

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
) -> int:
    """Run evaluator in configless mode — discover experiments from Redis registry.

    Connects to the Redis registry, discovers all registered experiments, sets
    up a verification engine from registration metadata, then starts a
    multi-queue supervisor across all discovered build/verify queues.

    Limitation: All experiments must share the same ``benchmarks_root`` and
    ``oss_fuzz_path`` on the evaluator machine.

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

    logger.info("=" * 60)
    logger.info("CRSBench Evaluator — Configless Mode")
    logger.info("=" * 60)
    logger.info(f"Redis host: {redis_host}")
    logger.info("Discovering experiments from registry...")
    logger.info("=" * 60)

    _redis_conn, experiments = discover_registered_experiments(redis_host)

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
    # and oss_fuzz_path since a single evaluator serves all experiments.
    first_reg = next(iter(experiments.values()))
    for name, reg in experiments.items():
        if reg.benchmarks_root != first_reg.benchmarks_root:
            logger.error(
                f"Experiment '{name}' has benchmarks_root='{reg.benchmarks_root}' "
                f"but '{first_reg.experiment}' has '{first_reg.benchmarks_root}'. "
                "All experiments on a configless evaluator must share the same paths. "
                "Use separate evaluator processes or --experiment-config mode."
            )
            return 1
        if reg.oss_fuzz_path != first_reg.oss_fuzz_path:
            logger.error(
                f"Experiment '{name}' has oss_fuzz_path='{reg.oss_fuzz_path}' "
                f"but '{first_reg.experiment}' has '{first_reg.oss_fuzz_path}'. "
                "All experiments on a configless evaluator must share the same paths. "
                "Use separate evaluator processes or --experiment-config mode."
            )
            return 1

    effective_benchmarks_root = benchmarks_root or first_reg.benchmarks_root
    oss_fuzz_path = first_reg.oss_fuzz_path
    per_pov_verify_timeout = max(
        reg.per_pov_verify_timeout for reg in experiments.values()
    )

    # Set up verification engine
    from crsbench.evaluation.verification.pov.engine import VerificationEngine

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=per_pov_verify_timeout,
    )

    from pathlib import Path

    from crsbench.distributed.evaluator_jobs import set_benchmarks_root, set_engine

    set_engine(engine)
    set_benchmarks_root(Path(effective_benchmarks_root))

    # Use stable experiment ordering so metadata precedence is deterministic.
    ordered_regs = [experiments[name] for name in sorted(experiments)]

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
    meta_cores_ordered = [
        reg.evaluator_cores for reg in ordered_regs if reg.evaluator_cores is not None
    ]
    meta_skip_ordered = [
        reg.evaluator_skip_cpus
        for reg in ordered_regs
        if reg.evaluator_skip_cpus is not None
    ]
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
        else (max(meta_build_cores) if meta_build_cores else 4)
    )
    resolved_verify_cores_per_job = (
        verify_cores_per_job
        if verify_cores_per_job is not None
        else (max(meta_verify_cores) if meta_verify_cores else 4)
    )
    resolved_verify_jobs = (
        verify_jobs
        if verify_jobs is not None
        else (
            max(meta_verify_jobs)
            if meta_verify_jobs
            else max(
                1,
                (resolved_build_jobs * resolved_build_cores_per_job)
                // resolved_verify_cores_per_job,
            )
        )
    )
    resolved_idle_timeout = (
        idle_timeout
        if idle_timeout is not None
        else (max(meta_idle_timeout) if meta_idle_timeout else 0)
    )

    resolved_cores = resolve_cli_or_first_metadata(
        cli_value=cores,
        metadata_values=meta_cores_ordered,
        field_name="evaluator.cores",
    )
    resolved_skip_cpus = resolve_cli_or_first_metadata(
        cli_value=skip_cpus,
        metadata_values=meta_skip_ordered,
        field_name="evaluator.skip_cpus",
    )
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
        "Evaluator resource profile (CLI > metadata > default): "
        f"build_jobs={resolved_build_jobs}, "
        f"build_cores_per_job={resolved_build_cores_per_job}, "
        f"verify_jobs={resolved_verify_jobs}, "
        f"verify_cores_per_job={resolved_verify_cores_per_job}, "
        f"cores={resolved_cores}, skip_cpus={resolved_skip_cpus}, "
        f"cpu_tag={resolved_cpu_tag}, idle_timeout={resolved_idle_timeout}"
    )

    logger.info("Starting multi-queue supervisor (build + verify)...")

    incompatible_path_warned: set[str] = set()

    def _refresh_evaluator_queues(redis_conn):
        from crsbench.distributed.registry import RegistryClient

        registry = RegistryClient(redis_conn)
        refreshed = registry.list_experiments()
        if not refreshed:
            return [], []

        compatible = []
        for name in sorted(refreshed):
            reg = refreshed[name]
            if (
                reg.benchmarks_root != first_reg.benchmarks_root
                or reg.oss_fuzz_path != first_reg.oss_fuzz_path
            ):
                if name not in incompatible_path_warned:
                    logger.warning(
                        "Skipping experiment due to incompatible shared paths: "
                        f"{name} (benchmarks_root={reg.benchmarks_root}, "
                        f"oss_fuzz_path={reg.oss_fuzz_path})"
                    )
                    incompatible_path_warned.add(name)
                continue
            compatible.append(reg)

        return (
            sorted({reg.build_queue for reg in compatible}),
            sorted({reg.verify_queue for reg in compatible}),
        )

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
        cores=resolved_cores,
        skip_cpus=resolved_skip_cpus,
        idle_timeout=resolved_idle_timeout,
        queue_refresher=_refresh_evaluator_queues,
        cpu_tag=resolved_cpu_tag,
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
    """Run evaluator in CI mode (no experiment config required).

    Listens on crsbench_ci_build and crsbench_ci_verify queues.

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

    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for evaluator execution")
        logger.error("Install with: pip install redis rq")
        return 1
    normalized_redis_host = normalize_redis_host(redis_host)
    if normalized_redis_host is None:
        logger.error(
            "CI evaluator requires a Redis host. "
            "Set CRSBENCH_REDIS_HOST to a non-empty hostname."
        )
        return 1
    redis_host = normalized_redis_host

    resolved_build_jobs = build_jobs if build_jobs is not None else 1
    resolved_build_cores_per_job = (
        build_cores_per_job if build_cores_per_job is not None else 4
    )
    resolved_verify_cores_per_job = (
        verify_cores_per_job if verify_cores_per_job is not None else 4
    )
    resolved_verify_jobs = (
        verify_jobs
        if verify_jobs is not None
        else max(
            1,
            (resolved_build_jobs * resolved_build_cores_per_job)
            // resolved_verify_cores_per_job,
        )
    )
    resolved_idle_timeout = idle_timeout if idle_timeout is not None else 0

    logger.info("=" * 60)
    logger.info("CRSBench Evaluator — CI Mode")
    logger.info("=" * 60)
    logger.info(f"Redis host: {redis_host}")
    logger.info(
        f"Build jobs: {resolved_build_jobs} x {resolved_build_cores_per_job} CPUs"
    )
    logger.info(
        f"Verify jobs: {resolved_verify_jobs} x {resolved_verify_cores_per_job} CPUs"
    )
    logger.info("Queues: crsbench_ci_build + crsbench_ci_verify")
    logger.info("=" * 60)

    from crsbench.distributed.ci_supervisor import run_ci_supervisor

    return run_ci_supervisor(
        redis_host=redis_host,
        build_queue_name="crsbench_ci_build",
        verify_queue_name="crsbench_ci_verify",
        worker_name=worker_name,
        build_jobs=resolved_build_jobs,
        build_cores_per_job=resolved_build_cores_per_job,
        verify_cores_per_job=resolved_verify_cores_per_job,
        verify_jobs=resolved_verify_jobs,
        job_runner=_evaluator_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
        cpu_tag=cpu_tag,
        idle_timeout=resolved_idle_timeout,
    )
