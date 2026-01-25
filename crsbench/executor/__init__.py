"""DAG executor for parallel job scheduling.

Provides a single executor that schedules jobs with explicit dependency
edges and bounded parallelism via --max-parallel.
"""

from crsbench.executor.dag import DAGExecutor
from crsbench.executor.errors import CycleError, DependencyError
from crsbench.executor.types import ExecutorResult, JobStatus

__all__ = [
    "CycleError",
    "DAGExecutor",
    "DependencyError",
    "ExecutorResult",
    "JobStatus",
]
