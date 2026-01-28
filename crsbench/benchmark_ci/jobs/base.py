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
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
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
    Fine-grained jobs (BuildJob, VerifyPovJob, etc.) require builder/infra.
    CI-level check jobs store their own references and use JobContext() empty.

    The `shared` dict allows inter-job data passing (e.g., build results
    consumed by downstream verify jobs).
    """

    builder: Optional["OSSFuzzBuilder"] = None
    infra: Optional["OSSFuzzInfrastructure"] = None
    timeout: int = 120
    shared: dict[str, Any] = field(default_factory=dict)
    output_dir: Optional[Path] = None


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

    def _job_log_path(self, context: JobContext) -> Optional[Path]:
        """Get log file path for this job, or None if no output_dir."""
        if not context.output_dir:
            return None
        parts = self.job_id.split(":")
        benchmark_name = parts[1] if len(parts) > 1 else "unknown"
        job_dir = context.output_dir / benchmark_name / self.job_type
        job_dir.mkdir(parents=True, exist_ok=True)
        safe_id = self.job_id.replace(":", "-")
        return job_dir / f"{safe_id}.log"

    def _write_job_log(self, context: JobContext, result: JobResult) -> None:
        """Write job execution log to output_dir if configured."""
        log_path = self._job_log_path(context)
        if not log_path:
            return
        import json as _json

        sep = "=" * 60
        status = "SUCCESS" if result.success else "FAILED"
        lines = [
            sep,
            f"Job:     {result.job_id}",
            f"Type:    {result.job_type}",
            f"Status:  {status}",
            f"Elapsed: {result.elapsed_seconds:.1f}s",
            sep,
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
            lines.append("")
        if result.details:
            lines.append(_json.dumps(result.details, indent=2, default=str))
            lines.append("")
        log_path.write_text("\n".join(lines) + "\n")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.job_id})"
