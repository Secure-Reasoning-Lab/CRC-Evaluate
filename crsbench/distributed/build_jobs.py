"""Distributed build job execution for CI builds.

Provides RQ-compatible functions for executing BuildSingleVariantJob
remotely via Redis workers. Used by ``crsbench ci build --distributed``.
"""

from __future__ import annotations

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
        patches=[Path(p) for p in params.get("patches", [])],
        use_inc_build=params.get("use_inc_build", True),
        force_rebuild=params.get("force_rebuild", False),
        skip_if_cached=params.get("skip_if_cached", False),
        source_mode=params.get("source_mode", "main_repo"),
        sanitizer=params.get("sanitizer", "address"),
        repo_name=params.get("repo_name"),
        project_image_prefix=params.get("project_image_prefix", "aixcc-afc"),
    )

    context = JobContext()
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
