"""Job classes for benchmark CI.

This module provides job abstractions for two-phase benchmark validation:
- BuildJob: Build a variant (tracked with time, logs)
- VerifyPovJob: Verify a POV against a variant
- VerifyPatchJob: Verify a patch fixes all POVs for a CPV
- BuildPatchJob: Apply a patch and rebuild (DAG node)
- TestPatchJob: Run POVs against a patched build (DAG node)
- CollectCoverageJob: Collect coverage data for a variant (DAG node)
- PovCheckJob: CI-level POV check (DAG node wrapping validator)
- PatchRtsCheckJob: CI-level patch+RTS check (DAG node wrapping validator)
- CoverageCheckJob: CI-level coverage check (DAG node wrapping validator)
"""

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.jobs.build import BuildJob
from crsbench.benchmark_ci.jobs.ci_checks import (
    CoverageCheckJob,
    PatchRtsCheckJob,
    PovCheckJob,
    RtsCheckJob,
)
from crsbench.benchmark_ci.jobs.coverage import CollectCoverageJob
from crsbench.benchmark_ci.jobs.patch import BuildPatchJob, TestPatchJob
from crsbench.benchmark_ci.jobs.verify import VerifyPatchJob, VerifyPovJob

__all__ = [
    "BuildJob",
    "BuildPatchJob",
    "CollectCoverageJob",
    "CoverageCheckJob",
    "Job",
    "JobContext",
    "JobResult",
    "PatchRtsCheckJob",
    "PovCheckJob",
    "RtsCheckJob",
    "TestPatchJob",
    "VerifyPatchJob",
    "VerifyPovJob",
]
