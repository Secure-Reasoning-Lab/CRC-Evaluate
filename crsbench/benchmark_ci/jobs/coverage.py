"""Coverage job for benchmark CI.

CollectCoverageJob: Collect coverage data for a variant after build.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult


@dataclass
class CollectCoverageJob(Job):
    """Collect coverage data for a variant.

    Runs after the base build completes. Collects coverage information
    using the built variant's harness and corpus.

    Attributes:
        benchmark: Benchmark name
        sanitizer: Sanitizer type
        variant_type: Variant type (e.g., "deltaref", "coverage")
        harness: Harness name for coverage collection
        corpus_dir: Optional path to corpus directory
    """

    benchmark: str
    sanitizer: str
    variant_type: str
    harness: str
    corpus_dir: Optional[Path] = None

    @property
    def job_id(self) -> str:
        return f"collect-coverage:{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    @property
    def job_type(self) -> str:
        return "collect-coverage"

    @property
    def depends_on(self) -> list[str]:
        """Depends on the build for this variant."""
        return [f"build:{self.benchmark}-{self.sanitizer}-{self.variant_type}"]

    @property
    def variant_name(self) -> str:
        return f"{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    def execute(self, context: JobContext) -> JobResult:
        """Collect coverage data for the variant."""
        started_at = datetime.now()

        try:
            coverage_fn = getattr(context.infra, "coverage", None)
            if coverage_fn is None:
                raise NotImplementedError(
                    "Coverage collection not yet wired to infrastructure"
                )

            coverage_fn(
                project_name=self.variant_name,
                harness=self.harness,
                corpus_dir=self.corpus_dir,
                timeout=context.timeout,
            )

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=True,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                details={
                    "variant_name": self.variant_name,
                    "harness": self.harness,
                },
            )

        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
