"""Job classes for benchmark CI.

This module provides job abstractions for benchmark validation:

Flat DAG jobs (used by CLI commands):
- BuildSingleVariantJob: Build a single variant for a benchmark (enables parallel builds)
- BuildVariantsJob: Build all variants for a benchmark (legacy, sequential)
- VerifyCpvPovJob: Verify POVs for a single CPV
- BuildPatchVariantJob: Build a patched variant
- PatchVariantTestJob: Run POVs + tests on a patched build
- FlatCollectCoverageJob: Collect coverage for a benchmark

Base classes:
- Job: Abstract base for all jobs
- JobContext: Shared context passed to job execution
- JobResult: Result of job execution
"""

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildSingleVariantJob,
    BuildVariantsJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
    VerifyCpvPovJob,
)

__all__ = [
    "BuildPatchVariantJob",
    "BuildSingleVariantJob",
    "BuildVariantsJob",
    "FlatCollectCoverageJob",
    "Job",
    "JobContext",
    "JobResult",
    "PatchVariantTestJob",
    "VerifyCpvPovJob",
]
