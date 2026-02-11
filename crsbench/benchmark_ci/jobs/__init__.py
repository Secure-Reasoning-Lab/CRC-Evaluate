"""Job classes for benchmark CI.

This module provides job abstractions for benchmark validation:

Flat jobs (executed via Redis distributed workers):
- BuildSingleVariantJob: Build a single variant for a benchmark (enables parallel builds)
- VerifyCpvPovJob: Verify POVs for a single CPV
- VerifyCpvVarJob: Verify variant POVs for a single CPV
- BuildPatchVariantJob: Build a patched variant
- PatchVariantTestJob: Run POVs + tests on a patched build
- PatchPovTestJob: Run POV test on patch
- PatchVarTestJob: Run variant test on patch
- PatchUnitTestJob: Run unit tests on patch
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
    FlatCollectCoverageJob,
    PatchVariantTestJob,
    VerifyCpvPovJob,
)

__all__ = [
    "BuildPatchVariantJob",
    "BuildSingleVariantJob",
    "FlatCollectCoverageJob",
    "Job",
    "JobContext",
    "JobResult",
    "PatchVariantTestJob",
    "VerifyCpvPovJob",
]
