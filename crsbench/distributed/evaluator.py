"""Evaluator process for distributed build and verify execution.

This module implements the evaluator process that:
1. Listens on both build and verify Redis queues (build has priority)
2. Runs build jobs to create variant Docker images
3. Runs POV verification against built variants
4. Stores results as RQ job results

No startup build phase — builds arrive via the build queue from
VariantPlanner in ci build, ci all, or crsbench run.
"""

import multiprocessing
import os
import sys
import time
from pathlib import Path
from typing import Optional, Union

from crsbench.utils.logger import configure_logger, get_logger

try:
    import redis
    import rq

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = get_logger(__name__)


def run_evaluator_main(
    config,
    experiment_name: str,
    redis_host: str = "localhost",
    max_jobs: int = 1,
    *,
    use_cpuset: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
    build_jobs: Optional[int] = None,
    build_cores_per_job: Optional[int] = None,
    verify_jobs: Optional[int] = None,
) -> int:
    """Main entry point for the evaluator process.

    Starts the dual-queue supervisor that processes both build and verify
    jobs. Build queue has priority over verify queue.

    Args:
        config: ExperimentConfig instance
        experiment_name: Experiment identifier for queue naming
        redis_host: Redis server hostname
        max_jobs: Maximum number of parallel jobs
        use_cpuset: Enable CPU affinity for jobs
        cores: CPU cores for evaluator pool (integer count or cpuset string)
        skip_cpus: CPUs to exclude from allocation (cpuset format)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for evaluator execution")
        logger.error("Install with: pip install redis rq")
        return 1

    logger.info("=" * 60)
    logger.info("CRSBench Distributed Evaluator")
    logger.info("=" * 60)
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Parallel jobs: {max_jobs}")
    logger.info(f"CPU affinity: {'enabled' if use_cpuset else 'disabled'}")
    logger.info("Queues: build (priority) + verify")
    logger.info("=" * 60)

    # Create verification engine for lazy verify use
    from crsbench.evaluation.verification.pov.engine import VerificationEngine

    oss_fuzz_path = Path(
        os.environ.get("CRSBENCH_EVALUATOR_OSS_FUZZ_PATH") or str(config.oss_fuzz_path)
    )

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=config.reproduce_timeout
        if hasattr(config, "reproduce_timeout")
        else 180,
    )

    # Set engine for lazy build loading — builds come via Redis queue
    from crsbench.distributed.evaluator_jobs import set_engine

    set_engine(engine)

    # Start dual-queue supervisor
    if build_jobs is not None:
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        logger.info("Starting CI dual-queue supervisor (build + verify)...")
        return run_ci_supervisor(
            redis_host=redis_host,
            build_queue_name=f"crsbench_{experiment_name}_build",
            verify_queue_name=f"crsbench_{experiment_name}_verify",
            worker_name=f"evaluator-{experiment_name}",
            build_jobs=build_jobs,
            build_cores_per_job=build_cores_per_job or 1,
            verify_jobs=verify_jobs or build_jobs,
            job_runner=_evaluator_job_runner,
            use_cpuset=use_cpuset,
            use_cgroups=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
        )

    logger.info("Starting dual-queue supervisor (build + verify)...")
    return _run_evaluator_supervisor(
        redis_host=redis_host,
        experiment_name=experiment_name,
        max_jobs=max_jobs,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
    )


def _get_benchmark_names(config) -> list[str]:
    """Extract benchmark names from experiment config.

    Args:
        config: ExperimentConfig instance

    Returns:
        List of benchmark name strings
    """
    if config.benchmarks:
        names = []
        for entry in config.benchmarks:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict):
                names.extend(entry.keys())
        return names

    return []


def _run_evaluator_supervisor(
    redis_host: str,
    experiment_name: str,
    max_jobs: int,
    *,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
) -> int:
    """Supervisor that dequeues build and verify jobs and spawns children.

    Listens on both build and verify queues. Build queue has priority:
    when both queues have jobs, build jobs are dequeued first.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        max_jobs: Maximum concurrent jobs
        use_cpuset: Enable CPU affinity
        use_cgroups: Create per-job cgroups with cpuset constraints

    Returns:
        Exit code (0 for success)
    """
    cpu_pool = None
    if use_cpuset:
        from crsbench.utils.cpu_pool import CPUPool

        # Parse cores: if it looks like an integer, pass as int
        cores_arg: Union[str, int, None] = None
        if cores is not None:
            try:
                cores_arg = int(cores)
            except ValueError:
                cores_arg = cores  # cpuset string
        cpu_pool = CPUPool(cores=cores_arg, skip_cpus=skip_cpus)

    # Cgroup initialization (if enabled)
    cgroup_base: Optional[Path] = None
    if use_cgroups:
        from crsbench.utils.cgroup import (
            cleanup_stale_cgroups,
            run_preflight_checks,
            setup_cgroup_hierarchy,
        )

        cgroup_base = run_preflight_checks()  # Raises CgroupError on failure
        setup_cgroup_hierarchy(cgroup_base)
        cleaned = cleanup_stale_cgroups(cgroup_base)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale cgroup(s)")

    workers: dict[
        int, tuple[multiprocessing.Process, list[int], str, Optional[Path]]
    ] = {}
    # pid -> (process, cpus, job_id, cgroup_path)

    try:
        # Connect to Redis
        redis_password = os.environ.get("REDIS_PASSWORD") or None
        redis_conn = redis.Redis(
            host=redis_host,
            password=redis_password,
            socket_connect_timeout=5,
        )
        redis_conn.ping()

        build_queue_name = f"crsbench_{experiment_name}_build"
        verify_queue_name = f"crsbench_{experiment_name}_verify"
        build_queue = rq.Queue(build_queue_name, connection=redis_conn)
        verify_queue = rq.Queue(verify_queue_name, connection=redis_conn)

        logger.info(
            f"Evaluator listening on queues: {build_queue_name} (priority), {verify_queue_name}"
        )
        if cpu_pool:
            logger.info(f"CPU pool initialized with {cpu_pool.total_cpus} CPUs")

        logger.info("Listening for jobs...")

        while True:
            # Cleanup finished workers
            for pid in list(workers.keys()):
                proc, cpus, _job_id, cgroup_path_entry = workers[pid]
                if not proc.is_alive():
                    proc.join()
                    if cpu_pool and cpus:
                        cpu_pool.release(cpus)
                        logger.info(
                            f"Worker (PID: {pid}) finished, released CPUs {cpus}"
                        )
                    if cgroup_path_entry:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path_entry)
                    del workers[pid]

            # Check for jobs and capacity
            build_count = build_queue.count
            verify_count = verify_queue.count
            total_queued = build_count + verify_count

            if total_queued > 0 and len(workers) < max_jobs:
                # Build queue has priority: dequeue from [build, verify] order
                result = rq.Queue.dequeue_any(
                    [build_queue, verify_queue],
                    timeout=None,
                    connection=redis_conn,
                )

                if result:
                    job, queue_obj = result
                    queue_label = (
                        "build" if queue_obj.name == build_queue_name else "verify"
                    )
                    cpu_count = job.meta.get("cpu_count", 2)

                    # Allocate CPUs if using cpuset
                    cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None

                    if cpu_pool is None or cpus is not None:
                        cpuset_str = ""
                        if cpus:
                            from crsbench.utils.cpu_pool import format_cpuset

                            cpuset_str = format_cpuset(cpus)
                            job.meta["allocated_cpus"] = cpuset_str
                            job.save_meta()

                        # Create cgroup for this job (if cgroups enabled)
                        cgroup_path: Optional[Path] = None
                        if cgroup_base is not None and cpuset_str:
                            from crsbench.utils.cgroup import (
                                cgroup_path_for_docker,
                                create_cgroup,
                            )

                            cgroup_name = f"{queue_label}-{job.id[:8]}"
                            cgroup_path = create_cgroup(
                                cgroup_base,
                                cgroup_name,
                                cpuset=cpuset_str,
                            )
                            cgroup_parent = cgroup_path_for_docker(cgroup_path)
                            job.meta["cgroup_parent"] = cgroup_parent
                            job.save_meta()
                            logger.info(
                                f"Created cgroup {cgroup_name} with cpuset={cpuset_str}"
                            )

                        # Set OSS_FUZZ_CGROUP_PARENT for child process
                        if cgroup_path is not None:
                            from crsbench.utils.cgroup import cgroup_path_for_docker

                            os.environ["OSS_FUZZ_CGROUP_PARENT"] = (
                                cgroup_path_for_docker(cgroup_path)
                            )

                        p = multiprocessing.Process(
                            target=_run_single_job,
                            args=(redis_host, job.id),
                        )
                        p.start()

                        # Unset after spawning so it doesn't leak to the next job
                        if cgroup_path is not None:
                            os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)

                        if p.pid is not None:
                            workers[p.pid] = (
                                p,
                                cpus or [],
                                job.id,
                                cgroup_path,
                            )

                        logger.info(
                            f"Started {queue_label} job {job.id[:8]} (PID: {p.pid})"
                            + (f" with CPUs {cpus}" if cpus else "")
                        )
                    else:
                        # Not enough CPUs, re-enqueue to the original queue
                        queue_obj.enqueue_job(job, at_front=True)
                        logger.debug(
                            f"Job {job.id[:8]} needs {cpu_count} CPUs, "
                            f"only {cpu_pool.available_count()} available. Re-enqueued."
                        )

            # Brief sleep to avoid busy-waiting
            time.sleep(0.5)

    except KeyboardInterrupt:
        logger.info("\nReceived interrupt signal, terminating workers...")
        for _pid, (p, _cpus, _job_id, _cg) in workers.items():
            if p.is_alive():
                p.terminate()
        for pid, (p, cpus, _job_id, cgroup_path_entry) in workers.items():
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing worker (PID: {pid})")
                p.kill()
                p.join()
            if cpu_pool and cpus:
                cpu_pool.release(cpus)
            if cgroup_path_entry:
                from crsbench.utils.cgroup import cleanup_cgroup

                cleanup_cgroup(cgroup_path_entry)
        return 0
    except Exception as e:
        logger.error(f"Evaluator supervisor error: {e}", exc_info=True)
        return 3


def _evaluator_job_runner(
    redis_host: str,
    _child_name: str,
    job_id: str,
) -> None:
    """Adapter for ci_supervisor: delegates to _run_single_job."""
    _run_single_job(redis_host, job_id)


def _run_single_job(
    redis_host: str,
    job_id: str,
) -> None:
    """Execute a single job (build or verify) in a child process.

    Generic job runner that fetches an RQ job and calls perform().
    Works for both build and verify jobs.

    Args:
        redis_host: Redis server hostname
        job_id: RQ job ID to execute
    """
    import rq.utils
    from rq.executions import Execution
    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry, FinishedJobRegistry

    # Reconfigure logging in subprocess
    configure_logger(level=os.environ.get("LOG_LEVEL", "INFO").upper(), sink=sys.stdout)

    redis_password = os.environ.get("REDIS_PASSWORD") or None
    redis_conn = redis.Redis(host=redis_host, password=redis_password)

    try:
        job = rq.job.Job.fetch(job_id, connection=redis_conn)
        queue = rq.Queue(job.origin, connection=redis_conn)

        finished_registry = FinishedJobRegistry(queue=queue)
        failed_registry = FailedJobRegistry(queue=queue)

        logger.info(f"Evaluator executing job {job_id}")

        # Create execution and mark as STARTED
        execution = None
        with redis_conn.pipeline() as pipeline:
            job.prepare_for_execution("evaluator", pipeline=pipeline)
            execution = Execution.create(job, ttl=-1, pipeline=pipeline)
            pipeline.execute()

        try:
            result = job.perform()

            # Mark as FINISHED
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
