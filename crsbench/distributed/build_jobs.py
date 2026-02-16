"""Distributed build job execution for CI builds.

Provides RQ-compatible functions for executing BuildSingleVariantJob
remotely via Redis workers. Serialization and enqueue/poll logic has
been unified into ``ci_jobs.py``; this module retains only the
execution entry point used by RQ workers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


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

    return result.to_dict()
