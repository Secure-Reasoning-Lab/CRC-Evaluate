"""Distributed build job execution for CI builds.

Provides RQ-compatible functions for executing BuildSingleVariantJob
remotely via Redis workers. Used by all commands that need builds:
``crsbench ci build``, ``crsbench ci all``, and ``crsbench run``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def serialize_build_job(job: Any) -> dict[str, Any]:
    """Serialize a BuildSingleVariantJob for RQ enqueue.

    Converts Path objects to strings and enums to their string values
    so the payload is JSON-safe for Redis transport.

    Args:
        job: BuildSingleVariantJob instance

    Returns:
        Dict suitable for passing to ``execute_ci_build()``
    """
    return {
        "benchmark_path": str(job.benchmark_path),
        "benchmark_name": job.benchmark_name,
        "variant_type": job.variant_type.value,
        "commit": job.commit,
        "main_repo": job.main_repo,
        "mode": job.mode.value,
        "language": job.language,
        "cpv_num": job.cpv_num,
        "patch_id": job.patch_id,
        "pov_id": job.pov_id,
        "patches": [str(p) for p in job.patches],
        "use_inc_build": job.use_inc_build,
        "force_rebuild": job.force_rebuild,
        "skip_if_cached": job.skip_if_cached,
        "source_mode": job.source_mode,
        "sanitizer": job.sanitizer,
        "repo_name": job.repo_name,
        "project_image_prefix": job.project_image_prefix,
    }


def execute_ci_build(params: dict[str, Any]) -> dict[str, Any]:
    """Execute a CI build job via RQ.

    RQ-compatible entry point that deserializes BuildSingleVariantJob
    parameters, executes the build locally, and returns serialized results.

    Args:
        params: Serialized BuildSingleVariantJob parameters

    Returns:
        Dict with job_id, success, error, elapsed_seconds, details
    """
    from crsbench.benchmark_ci.jobs.base import JobContext
    from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
    from crsbench.builder.types import BenchmarkMode, VariantType

    job = BuildSingleVariantJob(
        benchmark_path=Path(params["benchmark_path"]),
        benchmark_name=params["benchmark_name"],
        variant_type=VariantType(params["variant_type"]),
        commit=params["commit"],
        main_repo=params["main_repo"],
        mode=BenchmarkMode(params["mode"]),
        language=params.get("language", "c"),
        cpv_num=params.get("cpv_num"),
        patch_id=params.get("patch_id"),
        pov_id=params.get("pov_id"),
        patches=[Path(p) for p in params.get("patches", [])],
        use_inc_build=params.get("use_inc_build", True),
        force_rebuild=params.get("force_rebuild", False),
        skip_if_cached=params.get("skip_if_cached", False),
        source_mode=params.get("source_mode", "pkgs"),
        sanitizer=params.get("sanitizer", "address"),
        repo_name=params.get("repo_name"),
        project_image_prefix=params.get("project_image_prefix", "aixcc-afc"),
    )

    context = JobContext()
    output_dir = params.get("output_dir")
    if output_dir:
        context.output_dir = Path(output_dir)
    result = job.execute(context)

    return {
        "job_id": result.job_id,
        "job_type": result.job_type,
        "success": result.success,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "details": result.details,
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "finished_at": result.finished_at.isoformat() if result.finished_at else None,
    }


def enqueue_and_poll_builds(
    jobs: list[Any],
    redis_host: str,
    queue_name: str = "crsbench_ci_build",
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Enqueue build jobs to Redis and poll until all complete.

    Shared implementation used by all commands that need builds:
    ``crsbench ci build``, ``crsbench ci all``, and ``crsbench run``.

    Args:
        jobs: List of BuildSingleVariantJob instances
        redis_host: Redis server hostname
        queue_name: Redis queue name (default: crsbench_ci_build)
        output_dir: Optional directory for per-job stdout/stderr logs on worker

    Returns:
        Dict mapping job_id -> serialized result dict
    """
    try:
        import redis
        import rq
    except ImportError as exc:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed builds. "
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

    # Enqueue all build jobs with deterministic IDs
    rq_jobs: dict[str, rq.job.Job] = {}
    for job in jobs:
        params = serialize_build_job(job)
        if output_dir:
            params["output_dir"] = output_dir
        try:
            rq_job = queue.enqueue(
                "crsbench.distributed.build_jobs.execute_ci_build",
                params,
                job_timeout=3600,
                result_ttl=-1,
                meta={"cpu_count": 1},
                job_id=job.job_id,
            )
            rq_jobs[job.job_id] = rq_job
            logger.info(f"Enqueued {job.job_id} as RQ job {rq_job.id[:8]}")
        except Exception:
            # Job with same ID already exists -- fetch existing job
            existing = rq.job.Job.fetch(job.job_id, connection=redis_conn)
            rq_jobs[job.job_id] = existing
            logger.info(
                f"Job {job.job_id} already exists (dedup), "
                f"status: {existing.get_status()}"
            )

    logger.info(f"Enqueued {len(rq_jobs)} build jobs, waiting for completion...")

    # Poll for results
    pending = set(rq_jobs.keys())
    while pending:
        for job_id in list(pending):
            rq_job = rq_jobs[job_id]
            rq_job.refresh()
            status = rq_job.get_status()
            if status in ("finished", "failed"):
                pending.discard(job_id)

        if pending:
            logger.info(
                f"Waiting for {len(pending)}/{len(rq_jobs)} distributed build jobs..."
            )
            time.sleep(5)

    # Collect raw results
    raw_results: dict[str, Any] = {}
    for job_id, rq_job in rq_jobs.items():
        rq_job.refresh()
        status = rq_job.get_status()

        if status == "finished" and rq_job.result:
            raw_results[job_id] = rq_job.result
        else:
            exc_info = rq_job.exc_info or "Unknown error"
            raw_results[job_id] = {
                "job_id": job_id,
                "job_type": "build",
                "success": False,
                "error": str(exc_info)[:500],
                "elapsed_seconds": 0.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
            }

    return raw_results


def raw_results_to_executor_results(
    raw_results: dict[str, Any],
) -> dict[str, Any]:
    """Convert raw Redis result dicts to ExecutorResult format.

    Args:
        raw_results: Dict mapping job_id -> serialized result dict

    Returns:
        Dict mapping job_id -> ExecutorResult
    """
    from crsbench.benchmark_ci.jobs.base import JobResult
    from crsbench.executor.types import ExecutorResult, JobStatus

    results: dict[str, ExecutorResult] = {}
    for job_id, r in raw_results.items():
        if r.get("success"):
            job_result = JobResult(
                job_id=r["job_id"],
                job_type=r.get("job_type", "build"),
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
