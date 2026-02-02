"""Local sequential executor for benchmark CI jobs.

Executes jobs in dependency order without Redis. Used as the default
execution path for CI commands; --distributed enables Redis instead.
"""

import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from crsbench.benchmark_ci.jobs.base import Job, JobContext
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def execute_jobs_locally(
    jobs: Sequence[Job],
    context: JobContext | None = None,
    output_dir: Path | None = None,
) -> dict[str, ExecutorResult]:
    """Execute jobs locally in dependency order.

    Topologically sorts jobs by depends_on, executes each sequentially,
    and populates context.shared for downstream jobs.

    Args:
        jobs: List of Job instances to execute
        context: Optional shared context; created if not provided
        output_dir: Optional directory for per-job stdout/stderr logs

    Returns:
        Dict mapping job_id to ExecutorResult
    """
    if not jobs:
        return {}

    if context is None:
        context = JobContext()

    if output_dir is not None:
        context.output_dir = output_dir

    results: dict[str, ExecutorResult] = {}
    job_map = {j.job_id: j for j in jobs}

    # Topological sort (Kahn's algorithm)
    in_degree: dict[str, int] = {j.job_id: 0 for j in jobs}
    for job in jobs:
        for dep_id in job.depends_on:
            if dep_id in job_map:
                in_degree[job.job_id] += 1

    queue: deque[str] = deque(jid for jid, deg in in_degree.items() if deg == 0)
    order: list[str] = []

    while queue:
        jid = queue.popleft()
        order.append(jid)
        for job in jobs:
            if jid in job.depends_on and job.job_id in in_degree:
                in_degree[job.job_id] -= 1
                if in_degree[job.job_id] == 0:
                    queue.append(job.job_id)

    # Jobs not reachable via topo sort (cycles) — append at end
    for jid in in_degree:
        if jid not in order:
            order.append(jid)

    logger.info(f"Local executor: {len(order)} jobs to execute")

    for jid in order:
        job = job_map[jid]

        # Check if all dependencies succeeded
        dep_failed = False
        for dep_id in job.depends_on:
            dep_result = results.get(dep_id)
            if dep_result and dep_result.status != JobStatus.SUCCESS:
                dep_failed = True
                break
            # Dependency outside this job set — skip check
            if dep_id not in job_map and dep_id not in results:
                continue

        if dep_failed:
            logger.info(f"  SKIP {jid} (dependency failed)")
            results[jid] = ExecutorResult(
                job_id=jid,
                status=JobStatus.DEP_FAILED,
                error="dependency failed",
            )
            continue

        logger.info(f"  RUN  {jid}")
        start = time.monotonic()
        try:
            job_result = job.execute(context)
            elapsed = time.monotonic() - start

            status = JobStatus.SUCCESS if job_result.success else JobStatus.FAILED
            results[jid] = ExecutorResult(
                job_id=jid,
                status=status,
                elapsed_seconds=elapsed,
                job_result=job_result,
                error=job_result.error,
            )

            # Store result in context.shared for downstream jobs
            if job_result.success:
                context.shared[jid] = job_result.details

            logger.info(
                f"  {'OK' if job_result.success else 'FAIL'} {jid} ({elapsed:.1f}s)"
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.info(f"  ERR  {jid} ({elapsed:.1f}s): {exc}")
            results[jid] = ExecutorResult(
                job_id=jid,
                status=JobStatus.FAILED,
                elapsed_seconds=elapsed,
                error=str(exc),
            )

    return results
