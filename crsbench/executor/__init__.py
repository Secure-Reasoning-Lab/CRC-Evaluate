"""Executor types for job scheduling.

Provides shared result types used by Redis-based distributed execution.
"""

from crsbench.executor.errors import CycleError, DependencyError
from crsbench.executor.types import ExecutorResult, JobStatus

__all__ = [
    "CycleError",
    "DependencyError",
    "ExecutorResult",
    "JobStatus",
]
