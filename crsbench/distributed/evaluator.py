"""Evaluator process for distributed POV verification.

This module implements the evaluator process that:
1. Builds all variant Docker images at startup
2. Listens on the Redis verify queue for verification jobs
3. Runs POV verification against pre-built variants
4. Stores verdicts as RQ job results
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
    build_workers: int = 4,
) -> int:
    """Main entry point for the evaluator process.

    Builds all variant images, then starts the verification supervisor.

    Args:
        config: ExperimentConfig instance
        experiment_name: Experiment identifier for queue naming
        redis_host: Redis server hostname
        max_jobs: Maximum number of parallel verify jobs
        use_cpuset: Enable CPU affinity for verify jobs
        cores: CPU cores for evaluator pool (integer count or cpuset string)
        skip_cpus: CPUs to exclude from allocation (cpuset format)
        build_workers: Number of parallel variant build workers

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
    logger.info(f"Parallel verify jobs: {max_jobs}")
    logger.info(f"CPU affinity: {'enabled' if use_cpuset else 'disabled'}")
    logger.info("=" * 60)

    # Phase 1: Build all variant images
    logger.info(f"Phase 1: Building variant images ({build_workers} workers)...")
    engine, built_results = _build_all_variants(config, build_workers=build_workers)

    if not built_results:
        logger.error("No variants were built successfully. Exiting.")
        return 1

    # Report build results
    total_benchmarks = len(built_results)
    total_variants = sum(len(v) for v in built_results.values())
    successful = sum(
        1 for br in built_results.values() for r in br.values() if r.success
    )
    logger.info(
        f"Build complete: {successful}/{total_variants} variants "
        f"across {total_benchmarks} benchmarks"
    )

    # Phase 2: Start verification supervisor
    logger.info("Phase 2: Starting verification supervisor...")
    return _run_evaluator_supervisor(
        engine=engine,
        built_results=built_results,
        redis_host=redis_host,
        experiment_name=experiment_name,
        max_jobs=max_jobs,
        use_cpuset=use_cpuset,
        use_cgroups=use_cpuset,
        cores=cores,
        skip_cpus=skip_cpus,
    )


def _build_all_variants(config, *, build_workers: int = 4) -> tuple:
    """Build all variant Docker images for benchmarks in the experiment config.

    Args:
        config: ExperimentConfig instance
        build_workers: Number of parallel build workers

    Returns:
        Tuple of (VerificationEngine, dict of benchmark_name -> build_results)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from crsbench.evaluation.verification.pov.engine import VerificationEngine

    # Resolve paths from evaluator overrides or config
    oss_fuzz_path = Path(
        os.environ.get("CRSBENCH_EVALUATOR_OSS_FUZZ_PATH") or str(config.oss_fuzz_path)
    )
    benchmarks_root = Path(
        os.environ.get("CRSBENCH_EVALUATOR_BENCHMARKS_ROOT")
        or str(config.benchmarks_root or "benchmarks")
    )

    logger.info(f"oss-fuzz path: {oss_fuzz_path}")
    logger.info(f"Benchmarks root: {benchmarks_root}")

    # Create verification engine
    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=config.reproduce_timeout
        if hasattr(config, "reproduce_timeout")
        else 180,
    )

    # Get benchmark list from config
    benchmark_names = _get_benchmark_names(config)
    if not benchmark_names:
        logger.error("No benchmarks found in experiment config")
        return engine, {}

    logger.info(f"Building variants for {len(benchmark_names)} benchmarks:")
    for name in benchmark_names:
        logger.info(f"  - {name}")

    # Build variants (parallel when build_workers > 1)
    built_results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=build_workers) as executor:
        futures = {}
        for benchmark_name in benchmark_names:
            benchmark_path = benchmarks_root / benchmark_name
            if not benchmark_path.exists():
                logger.error(f"Benchmark path not found: {benchmark_path}")
                continue
            future = executor.submit(
                _build_single_benchmark, engine, benchmark_path, benchmark_name
            )
            futures[future] = benchmark_name

        for future in as_completed(futures):
            name, results = future.result()
            if results is not None:
                built_results[name] = results

    return engine, built_results


def _build_single_benchmark(engine, benchmark_path: Path, benchmark_name: str):
    """Build variants for a single benchmark.

    Args:
        engine: VerificationEngine instance
        benchmark_path: Path to benchmark directory
        benchmark_name: Name of benchmark

    Returns:
        Tuple of (benchmark_name, results dict or None)
    """
    adapter = engine.load_adapter(benchmark_path)
    if adapter is None:
        logger.error(f"Failed to load adapter for {benchmark_name}")
        return benchmark_name, None

    try:
        logger.info(f"Building variants for {benchmark_name}...")
        results = engine.get_or_build_results(adapter)
        for variant_name, result in results.items():
            status = "OK" if result.success else "FAILED"
            logger.info(f"  {variant_name}: {status}")
        return benchmark_name, results
    except Exception as e:
        logger.error(f"Failed to build variants for {benchmark_name}: {e}")
        return benchmark_name, None


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
    engine,
    built_results: dict[str, dict],
    redis_host: str,
    experiment_name: str,
    max_jobs: int,
    *,
    use_cpuset: bool = False,
    use_cgroups: bool = False,
    cores: Optional[str] = None,
    skip_cpus: Optional[str] = None,
) -> int:
    """Supervisor that dequeues verify jobs and spawns child processes.

    Follows the same pattern as worker.py _run_supervisor() but consumes
    from the verify queue instead of the trial queue.

    Args:
        engine: VerificationEngine instance
        built_results: Pre-built variant results keyed by benchmark name
        redis_host: Redis server hostname
        experiment_name: Experiment identifier
        max_jobs: Maximum concurrent verify jobs
        use_cpuset: Enable CPU affinity
        use_cgroups: Create per-job cgroups with cpuset constraints
            so Docker containers inherit CPU pinning via
            OSS_FUZZ_CGROUP_PARENT (default: False)

    Returns:
        Exit code (0 for success)
    """
    from crsbench.distributed.evaluator_jobs import set_build_cache

    # Set module-level build cache for job execution
    set_build_cache(engine, built_results)

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

        verify_queue_name = f"crsbench_{experiment_name}_verify"
        verify_queue = rq.Queue(verify_queue_name, connection=redis_conn)

        logger.info(f"Evaluator connected to verify queue: {verify_queue_name}")
        if cpu_pool:
            logger.info(f"CPU pool initialized with {cpu_pool.total_cpus} CPUs")

        logger.info("Listening for verification jobs...")

        while True:
            # Cleanup finished workers
            for pid in list(workers.keys()):
                proc, cpus, _job_id, cgroup_path_entry = workers[pid]
                if not proc.is_alive():
                    proc.join()
                    if cpu_pool and cpus:
                        cpu_pool.release(cpus)
                        logger.info(
                            f"Verify worker (PID: {pid}) finished, released CPUs {cpus}"
                        )
                    if cgroup_path_entry:
                        from crsbench.utils.cgroup import cleanup_cgroup

                        cleanup_cgroup(cgroup_path_entry)
                    del workers[pid]

            # Check for jobs and capacity
            queue_count = verify_queue.count
            if queue_count > 0 and len(workers) < max_jobs:
                result = rq.Queue.dequeue_any(
                    [verify_queue],
                    timeout=None,
                    connection=redis_conn,
                )

                if result:
                    job, _ = result
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

                        # Create cgroup for this verify job (if cgroups enabled)
                        cgroup_path: Optional[Path] = None
                        if cgroup_base is not None and cpuset_str:
                            from crsbench.utils.cgroup import (
                                cgroup_path_for_docker,
                                create_cgroup,
                            )

                            cgroup_name = f"verify-{job.id[:8]}"
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
                            target=_run_single_verify_job,
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
                            f"Started verify job {job.id[:8]} (PID: {p.pid})"
                            + (f" with CPUs {cpus}" if cpus else "")
                        )
                    else:
                        # Not enough CPUs, re-enqueue
                        verify_queue.enqueue_job(job, at_front=True)
                        logger.debug(
                            f"Verify job {job.id[:8]} needs {cpu_count} CPUs, "
                            f"only {cpu_pool.available_count()} available. Re-enqueued."
                        )

            # Brief sleep to avoid busy-waiting
            time.sleep(0.5)

    except KeyboardInterrupt:
        logger.info("\nReceived interrupt signal, terminating verify workers...")
        for _pid, (p, _cpus, _job_id, _cg) in workers.items():
            if p.is_alive():
                p.terminate()
        for pid, (p, cpus, _job_id, cgroup_path_entry) in workers.items():
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing verify worker (PID: {pid})")
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


def _run_single_verify_job(
    redis_host: str,
    job_id: str,
) -> None:
    """Execute a single verification job in a child process.

    This follows the same pattern as worker.py _run_single_job_worker().

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

        logger.info(f"Evaluator executing verify job {job_id}")

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

            logger.info(f"Verify job {job_id} completed successfully")

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

            logger.error(f"Verify job {job_id} failed: {e}", exc_info=True)
            raise

    except Exception as e:
        logger.error(f"Evaluator worker error: {e}", exc_info=True)
        raise
