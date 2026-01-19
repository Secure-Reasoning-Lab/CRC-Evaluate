"""Base classes for benchmark CI jobs.

This module provides the core abstractions for the job-based CI system:
- Job: Abstract base class for all jobs
- JobResult: Result of job execution with timing and logs
- JobContext: Shared context passed to job execution
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from crsbench.builder import OSSFuzzBuilder, OSSFuzzInfrastructure


@dataclass
class JobResult:
    """Result of a job execution.

    Tracks timing, success status, logs, and artifacts for any job type.
    """

    job_id: str
    job_type: str  # "build", "verify-pov", "verify-patch"
    success: bool
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    logs: str = ""
    error: Optional[str] = None
    artifacts: dict[str, Path] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "success": self.success,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "elapsed_seconds": self.elapsed_seconds,
            "logs": self.logs,
            "error": self.error,
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
            "details": self.details,
        }


@dataclass
class JobContext:
    """Shared context for job execution.

    Provides access to builder, infrastructure, and configuration.
    """

    builder: OSSFuzzBuilder
    infra: OSSFuzzInfrastructure
    timeout: int = 120


class Job(ABC):
    """Abstract base class for CI jobs.

    All jobs declare:
    - job_id: Unique identifier
    - job_type: Type of job (build, verify-pov, verify-patch)
    - depends_on: List of job IDs this job depends on

    Jobs implement execute() to perform their work.
    """

    @property
    @abstractmethod
    def job_id(self) -> str:
        """Unique identifier for this job.

        Format: {job_type}:{benchmark}-{sanitizer}-{variant}[:{pov_id}]
        Examples:
            - build:curl-asan-deltabase
            - verify-pov:curl-asan-deltaref:pov_0
            - verify-patch:curl-asan-cpv0
        """
        ...

    @property
    @abstractmethod
    def job_type(self) -> str:
        """Type of job: build, verify-pov, verify-patch."""
        ...

    @property
    def depends_on(self) -> list[str]:
        """Job IDs this job depends on.

        Verify jobs depend on build jobs. Build jobs have no dependencies.
        """
        return []

    @abstractmethod
    def execute(self, context: JobContext) -> JobResult:
        """Execute the job.

        Args:
            context: Shared context with builder and infrastructure

        Returns:
            JobResult with success status, timing, and details
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.job_id})"
