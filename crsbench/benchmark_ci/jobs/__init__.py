"""Job classes for benchmark CI.

This module provides job abstractions for two-phase benchmark validation:
- BuildJob: Build a variant (tracked with time, logs)
- VerifyPovJob: Verify a POV against a variant
- VerifyPatchJob: Verify a patch fixes all POVs for a CPV
"""

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.jobs.build import BuildJob
from crsbench.benchmark_ci.jobs.verify import VerifyPatchJob, VerifyPovJob

__all__ = [
    "Job",
    "JobContext",
    "JobResult",
    "BuildJob",
    "VerifyPovJob",
    "VerifyPatchJob",
]
