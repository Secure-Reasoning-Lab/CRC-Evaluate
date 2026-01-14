"""Job executors for benchmark CI testing.

This submodule contains job executor classes that implement
specific test operations for benchmarks.
"""

from crsbench.benchmark_ci.jobs.base import JobExecutor
from crsbench.benchmark_ci.jobs.coverage import CoverageCheckJob
from crsbench.benchmark_ci.jobs.delta import DeltaBasePovCheckJob, DeltaRefPovCheckJob
from crsbench.benchmark_ci.jobs.full import FullBasePovCheckJob
from crsbench.benchmark_ci.jobs.inc_build import IncBuildPullJob
from crsbench.benchmark_ci.jobs.patch import PatchCheckJob

__all__ = [
    "JobExecutor",
    # POV checks
    "DeltaBasePovCheckJob",
    "DeltaRefPovCheckJob",
    "FullBasePovCheckJob",
    # Patch check
    "PatchCheckJob",
    # Coverage check
    "CoverageCheckJob",
    # Inc-build pull
    "IncBuildPullJob",
]
