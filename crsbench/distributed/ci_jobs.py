"""Distributed CI verify/test job execution.

Provides RQ-compatible functions for executing CI verify and test jobs
remotely via Redis workers. Used by ``crsbench ci all`` to run verify/test
jobs on evaluator machines where Docker images exist.

Pattern: Same as build_jobs.py — serialize → enqueue → execute on evaluator → poll.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import rq
    import rq.job
    import rq.registry

    from crsbench.builder import BuildResult

logger = get_logger(__name__)

# Job class names that we support serializing/deserializing
_VERIFY_JOB_TYPES = frozenset(
    {
        "VerifyCpvPovJob",
        "VerifyCpvVarJob",
        "PatchPovTestJob",
        "PatchVarTestJob",
        "PatchUnitTestJob",
        "FlatCollectCoverageJob",
        "BuildPatchVariantJob",
    }
)


def serialize_ci_job(job: Any) -> dict[str, Any]:
    """Serialize a flat.py CI job for RQ enqueue.

    Converts Path objects to strings so the payload is JSON-safe
    for Redis transport.

    Args:
        job: Any flat.py Job instance (verify, patch, coverage)

    Returns:
        Dict suitable for passing to ``execute_ci_job()``
    """
    cls_name = type(job).__name__
    if cls_name not in _VERIFY_JOB_TYPES:
        raise ValueError(f"Unsupported job type for serialization: {cls_name}")

    params: dict[str, Any] = {"_job_class": cls_name}

    # Serialize dataclass fields based on job type
    if cls_name == "VerifyCpvPovJob":
        params.update(
            {
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "harness": job.harness,
                "benchmark_path": str(job.benchmark_path)
                if job.benchmark_path
                else None,
                "pov_path": str(job.pov_path) if job.pov_path else None,
                "build_job_ids": job.build_job_ids,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "VerifyCpvVarJob":
        params.update(
            {
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "harness": job.harness,
                "benchmark_path": str(job.benchmark_path)
                if job.benchmark_path
                else None,
                "pov_paths": [str(p) for p in job.pov_paths],
                "build_job_ids": job.build_job_ids,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchPovTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "pov_path": str(job.pov_path) if job.pov_path else None,
                "build_patch_job_id": job.build_patch_job_id,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchVarTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "pov_paths": [str(p) for p in job.pov_paths],
                "build_patch_job_id": job.build_patch_job_id,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchUnitTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "test_mode": job.test_mode,
                "build_patch_job_id": job.build_patch_job_id,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "FlatCollectCoverageJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "harness": job.harness,
                "build_job_id": job.build_job_id,
                "source_mode": job.source_mode,
                "build_job_ids": job.build_job_ids,
            }
        )
    elif cls_name == "BuildPatchVariantJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "patch_path": str(job.patch_path),
                "harness": job.harness,
                "sanitizer": job.sanitizer,
                "use_inc_build": job.use_inc_build,
                "force_rebuild": job.force_rebuild,
                "build_job_id": job.build_job_id,
                "source_mode": job.source_mode,
            }
        )

    return params


def _reconstruct_job(params: dict[str, Any]) -> Any:
    """Reconstruct a flat.py job from serialized parameters.

    Args:
        params: Serialized job parameters from serialize_ci_job()

    Returns:
        Reconstructed Job instance
    """
    from crsbench.benchmark_ci.jobs.flat import (
        BuildPatchVariantJob,
        FlatCollectCoverageJob,
        PatchPovTestJob,
        PatchUnitTestJob,
        PatchVarTestJob,
        VerifyCpvPovJob,
        VerifyCpvVarJob,
    )

    cls_name = params["_job_class"]

    if cls_name == "VerifyCpvPovJob":
        return VerifyCpvPovJob(
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            harness=params["harness"],
            benchmark_path=Path(params["benchmark_path"])
            if params.get("benchmark_path")
            else None,
            pov_path=Path(params["pov_path"]) if params.get("pov_path") else None,
            build_job_ids=params.get("build_job_ids", []),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "VerifyCpvVarJob":
        return VerifyCpvVarJob(
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            harness=params["harness"],
            benchmark_path=Path(params["benchmark_path"])
            if params.get("benchmark_path")
            else None,
            pov_paths=[Path(p) for p in params.get("pov_paths", [])],
            build_job_ids=params.get("build_job_ids", []),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchPovTestJob":
        return PatchPovTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            pov_path=Path(params["pov_path"]) if params.get("pov_path") else None,
            build_patch_job_id=params.get("build_patch_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchVarTestJob":
        return PatchVarTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            pov_paths=[Path(p) for p in params.get("pov_paths", [])],
            build_patch_job_id=params.get("build_patch_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchUnitTestJob":
        return PatchUnitTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            test_mode=params.get("test_mode", "FULL"),
            build_patch_job_id=params.get("build_patch_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "FlatCollectCoverageJob":
        return FlatCollectCoverageJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            harness=params["harness"],
            build_job_id=params.get("build_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
            build_job_ids=params.get("build_job_ids", []),
        )
    if cls_name == "BuildPatchVariantJob":
        return BuildPatchVariantJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            patch_path=Path(params["patch_path"]),
            harness=params.get("harness", ""),
            sanitizer=params.get("sanitizer", "address"),
            use_inc_build=params.get("use_inc_build", True),
            force_rebuild=params.get("force_rebuild", False),
            build_job_id=params.get("build_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
        )

    raise ValueError(f"Unknown job class: {cls_name}")


def _load_build_context_from_disk(
    context: Any,
    build_job_ids: list[str],
    benchmark_path: Path,
    source_mode: str,
) -> None:
    """Load build results from disk into context.shared.

    On the evaluator, Docker images already exist from previous build queue
    execution. This loads the BuildResult objects and adapter into
    context.shared so verify jobs can access them.

    Args:
        context: JobContext whose shared dict will be populated
        build_job_ids: Build job IDs that verify jobs depend on
        benchmark_path: Path to benchmark directory
        source_mode: Source mode for VerificationEngine
    """
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import get_oss_fuzz_root

    oss_fuzz_path = Path(get_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    adapter = engine.load_adapter(benchmark_path)
    if not adapter:
        logger.warning(f"Failed to load adapter for {benchmark_path}")
        return

    # Get build results for ALL sanitizers this benchmark uses
    build_results: dict[str, BuildResult] = {}
    for san in adapter.get_all_cpv_sanitizers():
        results = engine.get_or_build_results(adapter, sanitizer=san)
        build_results.update(results)

    if not build_results:
        logger.warning(f"No build results found for {benchmark_path.name}")
        return

    # Populate context.shared for each build_job_id
    # Build job IDs have format "build-single/{benchmark}/{variant_name}"
    for bid in build_job_ids:
        parts = bid.split("/")
        variant_name = parts[2] if len(parts) > 2 else None
        if variant_name and variant_name in build_results:
            context.shared[bid] = {
                "build_result": build_results[variant_name],
                "adapter": adapter,
            }
        else:
            logger.warning(
                f"Build result not found for {bid} (variant: {variant_name}). "
                f"Available: {list(build_results.keys())}"
            )

    logger.info(
        f"Loaded {len(context.shared)} build context entries for {benchmark_path.name}"
    )


def _load_patch_build_context(
    context: Any,
    job: Any,
    source_mode: str,
) -> None:
    """Load patch build context from disk for patch verify jobs.

    For patch verify/test jobs, the BuildPatchVariantJob already ran and
    stored its results. We reconstruct the expected context.shared entry.

    Args:
        context: JobContext whose shared dict will be populated
        job: Patch verify/test job with build_patch_job_id
        source_mode: Source mode for VerificationEngine
    """
    from crsbench.builder.types import BuildConfig, VariantType
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import get_oss_fuzz_root

    bid = job.build_patch_job_id
    if not bid:
        return

    oss_fuzz_path = Path(get_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    adapter = engine.load_adapter(job.benchmark_path)
    if not adapter:
        return

    # Compute expected variant_name for the patched build
    sanitizer = adapter.get_cpv_sanitizer(job.harness, job.cpv_id)
    config = BuildConfig(
        benchmark_name=job.benchmark_name,
        benchmark_path=job.benchmark_path,
        variant_type=VariantType.PATCHED,
        mode=adapter.get_mode(),
        sanitizer=sanitizer,
        language=adapter.lang,
        commit=adapter.get_ref_commit() or adapter.get_base_commit(),
        main_repo=adapter.main_repo,
        patch_id=job.patch_id,
        pov_id=job.cpv_id,
    )

    context.shared[bid] = {
        "variant_name": config.variant_name,
        "sanitizer": sanitizer,
        "fallback_used": False,
        "inc_build_available": False,
    }

    logger.info(f"Loaded patch build context for {bid}: variant={config.variant_name}")


def execute_ci_job(params: dict[str, Any]) -> dict[str, Any]:
    """Execute a CI verify/test job via RQ.

    RQ-compatible entry point that deserializes a CI job, pre-populates
    context.shared from disk (Docker images already exist from build queue),
    and returns serialized JobResult.

    Args:
        params: Serialized CI job parameters from serialize_ci_job()

    Returns:
        Dict with job result fields (from JobResult.to_dict())
    """
    from crsbench.benchmark_ci.jobs.base import JobContext

    job = _reconstruct_job(params)
    context = JobContext()
    output_dir = params.get("output_dir")
    if output_dir:
        context.output_dir = Path(output_dir)

    # Pre-populate context.shared from disk so verify jobs find build results
    source_mode = params.get("source_mode", "pkgs")

    if hasattr(job, "build_job_ids") and job.build_job_ids:
        benchmark_path = getattr(job, "benchmark_path", None)
        if benchmark_path:
            _load_build_context_from_disk(
                context, job.build_job_ids, benchmark_path, source_mode
            )

    if hasattr(job, "build_patch_job_id") and job.build_patch_job_id:
        _load_patch_build_context(context, job, source_mode)

    result = job.execute(context)
    return result.to_dict()


def _recover_orphaned_deferred_jobs(
    queue: "rq.Queue",
    rq_jobs: dict[str, rq.job.Job],
    pending: set[str],
) -> int:
    """Recover deferred jobs whose dependencies are all satisfied.

    RQ 2.x has a race condition where finished jobs clear their dependents'
    dependency sets but fail to move them from the deferred registry to the
    queue. This function detects such orphaned jobs and force-enqueues them.

    Returns number of recovered jobs.
    """
    import rq.registry
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RqJob
    from rq.job import JobStatus

    deferred_registry = rq.registry.DeferredJobRegistry(queue=queue)
    deferred_ids = set(deferred_registry.get_job_ids())

    # Only check jobs we're tracking
    orphaned_ids = deferred_ids & pending
    if not orphaned_ids:
        return 0

    recovered = 0
    for job_id in orphaned_ids:
        rq_job = rq_jobs[job_id]
        rq_job.refresh()
        if rq_job.get_status() != JobStatus.DEFERRED:
            continue

        # Check if all dependencies are finished
        all_deps_done = True
        for dep_id in rq_job.dependency_ids:
            try:
                dep_job = RqJob.fetch(dep_id, connection=queue.connection)
                if dep_job.get_status() != JobStatus.FINISHED:
                    all_deps_done = False
                    break
            except NoSuchJobError:
                logger.debug(f"Dependency {dep_id} not found in Redis")
                all_deps_done = False
                break

        if all_deps_done:
            # Force-enqueue atomically via pipeline to prevent partial state
            # if the process crashes between steps
            pipe = queue.connection.pipeline()
            deferred_registry.remove(rq_job, pipeline=pipe)
            rq_job.set_status(JobStatus.QUEUED, pipeline=pipe)
            # Clear internal dep list so RQ doesn't re-defer when processing
            rq_job._dependency_ids = []
            rq_job.save(pipeline=pipe)
            pipe.delete(rq_job.dependencies_key)
            queue.push_job_id(job_id, pipeline=pipe)
            pipe.execute()
            recovered += 1

    if recovered:
        logger.info(f"Recovered {recovered} orphaned deferred jobs")
    return recovered


def enqueue_and_poll_ci_jobs(
    jobs: list[Any],
    redis_host: str,
    queue_name: str = "crsbench_ci_verify",
    output_dir: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Enqueue CI verify/test jobs to Redis and poll until all complete.

    Follows same pattern as build_jobs.enqueue_and_poll_builds().
    Jobs are executed by evaluator workers where Docker images exist.

    Args:
        jobs: List of flat.py Job instances (verify, patch, coverage)
        redis_host: Redis server hostname
        queue_name: Redis queue name
        output_dir: Optional directory for per-job stdout/stderr logs on worker

    Returns:
        Dict mapping job_id -> serialized JobResult dict
    """
    try:
        import redis
        import rq
    except ImportError as exc:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed CI jobs. "
            "Install with: pip install redis rq"
        ) from exc

    redis_password = os.environ.get("REDIS_PASSWORD") or None
    redis_conn = redis.Redis(
        host=redis_host,
        password=redis_password,
        socket_connect_timeout=5,
    )
    redis_conn.ping()

    queue = rq.Queue(queue_name, connection=redis_conn)
    logger.info(f"Connected to Redis at {redis_host}, queue: {queue_name}")

    # Serialize and enqueue jobs respecting dependency order
    # Build-type jobs (BuildPatchVariantJob) go first, then verify jobs
    build_type_jobs = [j for j in jobs if j.job_type == "build"]
    verify_type_jobs = [j for j in jobs if j.job_type != "build"]
    ordered_jobs = build_type_jobs + verify_type_jobs

    rq_jobs: dict[str, rq.job.Job] = {}
    for job in ordered_jobs:
        params = serialize_ci_job(job)
        if output_dir:
            params["output_dir"] = output_dir

        # Map depends_on to RQ dependency IDs
        depends_on_rq: Optional[list[rq.job.Job]] = None
        if job.depends_on:
            dep_rq_jobs = [rq_jobs[d] for d in job.depends_on if d in rq_jobs]
            if dep_rq_jobs:
                depends_on_rq = dep_rq_jobs

        try:
            rq_job = queue.enqueue(
                "crsbench.distributed.ci_jobs.execute_ci_job",
                params,
                job_timeout=3600,
                result_ttl=-1,
                meta={"cpu_count": 1},
                job_id=job.job_id,
                depends_on=depends_on_rq,
            )
            rq_jobs[job.job_id] = rq_job
            logger.info(f"Enqueued CI job {job.job_id}")
        except Exception:
            existing = rq.job.Job.fetch(job.job_id, connection=redis_conn)
            rq_jobs[job.job_id] = existing
            logger.info(
                f"CI job {job.job_id} already exists, status: {existing.get_status()}"
            )

    logger.info(
        f"Enqueued {len(rq_jobs)} CI jobs ({len(build_type_jobs)} build, "
        f"{len(verify_type_jobs)} verify/test), waiting for completion..."
    )

    # Poll for results with deferred job recovery
    pending = set(rq_jobs.keys())
    recovery_interval = 30  # seconds between recovery sweeps
    last_recovery = time.monotonic()

    while pending:
        for job_id in list(pending):
            rq_job = rq_jobs[job_id]
            rq_job.refresh()
            status = rq_job.get_status()
            if status in ("finished", "failed"):
                pending.discard(job_id)

        # Periodically recover orphaned deferred jobs (RQ race condition)
        now = time.monotonic()
        if pending and (now - last_recovery) >= recovery_interval:
            try:
                _recover_orphaned_deferred_jobs(queue, rq_jobs, pending)
            except Exception:
                logger.warning(
                    "Recovery sweep failed, will retry next interval",
                    exc_info=True,
                )
            last_recovery = now

        if pending:
            logger.info(f"Waiting for {len(pending)}/{len(rq_jobs)} CI jobs...")
            time.sleep(5)

    # Collect results
    raw_results: dict[str, dict[str, Any]] = {}
    for job_id, rq_job in rq_jobs.items():
        rq_job.refresh()
        status = rq_job.get_status()

        if status == "finished" and rq_job.result:
            raw_results[job_id] = rq_job.result
        else:
            exc_info = rq_job.exc_info or "Unknown error"
            raw_results[job_id] = {
                "job_id": job_id,
                "job_type": "verify",
                "success": False,
                "error": str(exc_info)[:500],
                "elapsed_seconds": 0.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
            }

    return raw_results


def ci_results_to_executor_results(
    raw_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Convert raw Redis CI result dicts to ExecutorResult format.

    Args:
        raw_results: Dict mapping job_id -> serialized JobResult dict

    Returns:
        Dict mapping job_id -> ExecutorResult
    """
    from datetime import datetime

    from crsbench.benchmark_ci.jobs.base import JobResult
    from crsbench.executor.types import ExecutorResult, JobStatus

    results: dict[str, ExecutorResult] = {}
    for job_id, r in raw_results.items():
        if r.get("success"):
            job_result = JobResult(
                job_id=r["job_id"],
                job_type=r.get("job_type", "verify"),
                success=r["success"],
                started_at=datetime.fromisoformat(r["started_at"])
                if r.get("started_at")
                else datetime.now(),
                finished_at=datetime.fromisoformat(r["finished_at"])
                if r.get("finished_at")
                else datetime.now(),
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                error=r.get("error"),
                details=r.get("details", {}),
            )
            results[job_id] = ExecutorResult(
                job_id=job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                job_result=job_result,
            )
        else:
            results[job_id] = ExecutorResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error=r.get("error", "Unknown error"),
            )

    return results
