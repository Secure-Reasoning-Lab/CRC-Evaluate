"""Types for the DAG executor.

JobStatus tracks the lifecycle state of a job in the executor.
ExecutorResult captures the scheduling-level outcome for each job.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from crsbench.benchmark_ci.jobs.base import JobResult


class JobStatus(Enum):
    """Status of a job within the DAG executor."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DEP_FAILED = "dep_failed"


@dataclass
class ExecutorResult:
    """Scheduling-level result for a job in the DAG executor.

    This wraps the job's own JobResult with executor-level metadata:
    - Status tracks whether the job ran, failed, or was skipped due to dep failure
    - elapsed_seconds measures wall-clock time for the job execution
    - job_result holds the underlying result from job.execute() (None for DEP_FAILED)
    """

    job_id: str
    status: JobStatus
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    job_result: Optional[JobResult] = None

    @property
    def success(self) -> bool:
        """Whether the job completed successfully."""
        return self.status == JobStatus.SUCCESS
