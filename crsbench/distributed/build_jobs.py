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
    from crsbench.distributed.ci_jobs import _reconstruct_job

    job = _reconstruct_job(params)

    context = JobContext()
    output_dir = params.get("output_dir")
    if output_dir:
        context.output_dir = Path(output_dir)
    result = job.execute(context)

    return result.to_dict()
