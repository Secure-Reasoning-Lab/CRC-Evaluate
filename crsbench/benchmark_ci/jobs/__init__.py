"""Job classes for benchmark CI.

This module provides job abstractions for benchmark validation:
- BuildJob: Build a variant (tracked with time, logs)
- VerifyPovJob: Verify a POV against a variant
- VerifyPatchJob: Verify a patch fixes all POVs for a CPV
- BuildPatchJob: Apply a patch and rebuild (DAG node)
- TestPatchJob: Run POVs against a patched build (DAG node)
- CollectCoverageJob: Collect coverage data for a variant (DAG node)

Flat DAG jobs (replace coarse wrappers with per-CPV/per-patch atomic jobs):
- BuildVariantsJob: Build all variants for a benchmark
- VerifyCpvPovJob: Verify POVs for a single CPV
- BuildPatchVariantJob: Build a patched variant
- TestPatchVariantJob: Run POVs + tests on a patched build
- FlatCollectCoverageJob: Collect coverage for a benchmark
"""

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.jobs.build import BuildJob
from crsbench.benchmark_ci.jobs.coverage import CollectCoverageJob
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildVariantsJob,
    FlatCollectCoverageJob,
    TestPatchVariantJob,
    VerifyCpvPovJob,
)
from crsbench.benchmark_ci.jobs.patch import BuildPatchJob, TestPatchJob
from crsbench.benchmark_ci.jobs.verify import VerifyPatchJob, VerifyPovJob

__all__ = [
    "BuildJob",
    "BuildPatchJob",
    "BuildPatchVariantJob",
    "BuildVariantsJob",
    "CollectCoverageJob",
    "FlatCollectCoverageJob",
    "Job",
    "JobContext",
    "JobResult",
    "TestPatchJob",
    "TestPatchVariantJob",
    "VerifyCpvPovJob",
    "VerifyPatchJob",
    "VerifyPovJob",
]
