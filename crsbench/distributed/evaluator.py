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

    from crsbench.distributed.queue import (
        create_redis_connection,
        validate_queue_name_component,
    )

    validate_queue_name_component(experiment_name)

    redis_conn = create_redis_connection(redis_host)
    build_queue = rq.Queue(f"crsbench_{experiment_name}_build", connection=redis_conn)

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

    from crsbench.distributed.queue import validate_queue_name_component

    validate_queue_name_component(experiment_name)

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
        timeout=config.reproduce_timeout
        if hasattr(config, "reproduce_timeout")
        else 180,
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
        build_queue_name=f"crsbench_{experiment_name}_build",
        verify_queue_name=f"crsbench_{experiment_name}_verify",
        worker_name=f"evaluator-{experiment_name}",
        build_jobs=build_jobs or 1,
        build_cores_per_job=build_cores_per_job or 1,
        verify_cores_per_job=verify_cores_per_job or 1,
        verify_jobs=verify_jobs or (build_jobs or 1),
        job_runner=_evaluator_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
        idle_timeout=idle_timeout,
    )


def _evaluator_job_runner(
    redis_host: str,
    child_name: str,
    job_id: str,
) -> None:
    """Adapter for ci_supervisor: delegates to _run_single_job."""
    _run_single_job(redis_host, job_id, child_name=child_name)


def _run_single_job(
    redis_host: str,
    job_id: str,
    child_name: str = "",
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
    configure_logger(level=os.environ.get("LOG_LEVEL", "INFO").upper(), sink=sys.stdout)

    try:
        from crsbench.distributed.queue import create_redis_connection

        redis_conn = create_redis_connection(redis_host)

        job = rq.job.Job.fetch(job_id, connection=redis_conn)
        queue = rq.Queue(job.origin, connection=redis_conn)

        finished_registry = FinishedJobRegistry(queue=queue)
        failed_registry = FailedJobRegistry(queue=queue)

        label = f"[{child_name}] " if child_name else ""
        logger.info(f"{label}Evaluator executing job {job_id}")

        # Create execution and mark as STARTED
        execution = None
        with redis_conn.pipeline() as pipeline:
            job.prepare_for_execution("evaluator", pipeline=pipeline)
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


def run_evaluator_ci_mode(
    redis_host: str = "localhost",
    worker_name: str = "ci-evaluator",
    build_jobs: int = 1,
    build_cores_per_job: int = 1,
    verify_cores_per_job: int = 1,
    verify_jobs: Optional[int] = None,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    idle_timeout: int = 0,
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
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for evaluator execution")
        logger.error("Install with: pip install redis rq")
        return 1

    logger.info("=" * 60)
    logger.info("CRSBench Evaluator — CI Mode")
    logger.info("=" * 60)
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Build jobs: {build_jobs} x {build_cores_per_job} CPUs")
    logger.info(
        f"Verify jobs: {verify_jobs or build_jobs} x {verify_cores_per_job} CPUs"
    )
    logger.info("Queues: crsbench_ci_build + crsbench_ci_verify")
    logger.info("=" * 60)

    from crsbench.distributed.ci_supervisor import run_ci_supervisor

    return run_ci_supervisor(
        redis_host=redis_host,
        build_queue_name="crsbench_ci_build",
        verify_queue_name="crsbench_ci_verify",
        worker_name=worker_name,
        build_jobs=build_jobs,
        build_cores_per_job=build_cores_per_job,
        verify_cores_per_job=verify_cores_per_job,
        verify_jobs=verify_jobs or build_jobs,
        job_runner=_evaluator_job_runner,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
        idle_timeout=idle_timeout,
    )
