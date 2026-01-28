"""DAG executor for parallel job scheduling with dependency resolution.

Uses graphlib.TopologicalSorter for dependency ordering and a single
ThreadPoolExecutor for bounded parallelism. Replaces nested ThreadPoolExecutor
patterns with explicit dependency edges and a single --max-parallel knob.
"""

import graphlib
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.executor.errors import CycleError, DependencyError
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class DAGExecutor:
    """Executes jobs respecting dependency ordering with bounded parallelism.

    Jobs are scheduled via topological sort. At most max_workers jobs run
    concurrently. When a job fails, all transitive dependents are marked
    DEP_FAILED without execution.

    Supports typed concurrency limits via type_limits parameter:
        type_limits={"build": 2, "verify": 8} means at most 2 build jobs
        and 8 verify jobs run concurrently (pool size = sum = 10).
    """

    def __init__(
        self,
        max_workers: int = 4,
        type_limits: dict[str, int] | None = None,
    ) -> None:
        if type_limits:
            total = sum(type_limits.values())
            if total < 1:
                msg = f"sum of type_limits must be >= 1, got {total}"
                raise ValueError(msg)
            self.max_workers = total
            self.type_limits = type_limits
        else:
            if max_workers < 1:
                msg = f"max_workers must be >= 1, got {max_workers}"
                raise ValueError(msg)
            self.max_workers = max_workers
            self.type_limits: dict[str, int] | None = None

    def execute(
        self, jobs: list[Job], context: JobContext
    ) -> dict[str, ExecutorResult]:
        """Execute jobs respecting dependencies with bounded parallelism.

        When type_limits is set, per-type concurrency is enforced: ready jobs
        whose type is at capacity are held in a buffer until a slot frees.

        Args:
            jobs: List of jobs to execute. Each job declares depends_on.
            context: Shared context passed to each job's execute() method.

        Returns:
            Dict mapping job_id to ExecutorResult for every job.

        Raises:
            CycleError: If the dependency graph contains a cycle.
            DependencyError: If a job depends on an unknown job ID.
        """
        if not jobs:
            return {}

        job_map = {job.job_id: job for job in jobs}
        graph = {job.job_id: set(job.depends_on) for job in jobs}

        self._validate_dependencies(job_map, graph)

        ts = self._prepare_sorter(graph)

        results: dict[str, ExecutorResult] = {}
        failed_jobs: set[str] = set()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            in_flight: dict[str, Future[JobResult]] = {}
            ready_buffer: list[str] = []
            type_counts: dict[str, int] = (
                dict.fromkeys(self.type_limits, 0) if self.type_limits else {}
            )

            while ts.is_active():
                ready_buffer.extend(ts.get_ready())

                remaining: list[str] = []
                dep_failed_processed = False
                for job_id in ready_buffer:
                    if self._has_failed_dep(job_id, graph, failed_jobs):
                        results[job_id] = ExecutorResult(
                            job_id=job_id,
                            status=JobStatus.DEP_FAILED,
                            error="Dependency failed",
                        )
                        failed_jobs.add(job_id)
                        ts.done(job_id)
                        dep_failed_processed = True
                        logger.debug("Job %s: DEP_FAILED (dependency failed)", job_id)
                        continue

                    job = job_map[job_id]
                    if self._can_submit(job, type_counts):
                        logger.debug("Job %s: submitting", job_id)
                        future = pool.submit(self._run_job, job, context)
                        in_flight[job_id] = future
                        if self.type_limits:
                            type_counts[job.job_type] = (
                                type_counts.get(job.job_type, 0) + 1
                            )
                    else:
                        remaining.append(job_id)
                ready_buffer = remaining

                # DEP_FAILED may have unblocked new jobs via ts.done()
                if dep_failed_processed:
                    continue

                if not in_flight:
                    if ts.is_active() and not ready_buffer:
                        logger.error("Deadlock: no in-flight jobs but DAG still active")
                        break
                    continue

                # Wait for at least one job to complete
                for completed_future in as_completed(in_flight.values()):
                    completed_id = None
                    for jid, fut in in_flight.items():
                        if fut is completed_future:
                            completed_id = jid
                            break

                    if completed_id is None:
                        continue

                    job_result = completed_future.result()
                    elapsed = job_result.elapsed_seconds if job_result else 0.0

                    if job_result.success:
                        status = JobStatus.SUCCESS
                        logger.debug("Job %s: SUCCESS (%.1fs)", completed_id, elapsed)
                    else:
                        status = JobStatus.FAILED
                        failed_jobs.add(completed_id)
                        logger.debug(
                            "Job %s: FAILED (%.1fs) - %s",
                            completed_id,
                            elapsed,
                            job_result.error,
                        )

                    results[completed_id] = ExecutorResult(
                        job_id=completed_id,
                        status=status,
                        elapsed_seconds=elapsed,
                        error=job_result.error if not job_result.success else None,
                        job_result=job_result,
                    )

                    del in_flight[completed_id]
                    if self.type_limits:
                        completed_type = job_map[completed_id].job_type
                        type_counts[completed_type] = max(
                            0, type_counts.get(completed_type, 1) - 1
                        )
                    ts.done(completed_id)
                    break

        return results

    def _can_submit(self, job: Job, type_counts: dict[str, int]) -> bool:
        """Check if a job can be submitted given current type counts."""
        if not self.type_limits:
            return True
        jtype = job.job_type
        limit = self.type_limits.get(jtype)
        if limit is None:
            return True
        return type_counts.get(jtype, 0) < limit

    def _validate_dependencies(
        self,
        job_map: dict[str, Job],
        graph: dict[str, set[str]],
    ) -> None:
        """Validate all dependencies reference existing jobs."""
        all_ids = set(job_map.keys())
        for job_id, deps in graph.items():
            for dep in deps:
                if dep not in all_ids:
                    raise DependencyError(job_id, dep)

    def _prepare_sorter(
        self, graph: dict[str, set[str]]
    ) -> graphlib.TopologicalSorter[str]:
        """Create and prepare a TopologicalSorter, wrapping CycleError."""
        ts: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(graph)
        try:
            ts.prepare()
        except graphlib.CycleError as e:
            nodes = list(e.args[1]) if len(e.args) > 1 else []
            raise CycleError(nodes) from e
        return ts

    def _has_failed_dep(
        self,
        job_id: str,
        graph: dict[str, set[str]],
        failed_jobs: set[str],
    ) -> bool:
        """Check if any direct dependency of job_id has failed."""
        return bool(graph[job_id] & failed_jobs)

    def _run_job(self, job: Job, context: JobContext) -> JobResult:
        """Execute a single job, catching exceptions."""
        start = time.monotonic()
        try:
            return job.execute(context)
        except Exception as e:
            from datetime import datetime

            elapsed = time.monotonic() - start
            now = datetime.now()
            return JobResult(
                job_id=job.job_id,
                job_type=job.job_type,
                success=False,
                started_at=now,
                finished_at=now,
                elapsed_seconds=elapsed,
                error=str(e),
            )
