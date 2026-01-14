"""Data models for benchmark CI testing.

This module contains all data classes used in the benchmark CI module:
- Job context and configuration (JobContext, Task, ExecJobType)
- Benchmark metadata (Harness, Vulnerability, POV)
- Results and metrics (CIResult, IncBuildMetrics, ResultCollector)
"""

import csv
import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Enums
# =============================================================================


class TaskMode(enum.Enum):
    """Execution mode for benchmark tasks."""

    DELTA = "delta"
    FULL = "full"


class ExecJobType(enum.Enum):
    """Type of execution job for benchmark testing."""

    # POV verification checks
    DELTA_BASE_POV_CHECK = (
        "delta_base_pov_check"  # POV should NOT crash at base (clean)
    )
    DELTA_REF_POV_CHECK = "delta_ref_pov_check"  # POV should crash at ref (vulnerable)
    FULL_BASE_POV_CHECK = "full_base_pov_check"  # POV should crash at base (vulnerable)

    # Patch verification
    PATCH_CHECK = "patch_check"  # Verify patch fixes the vulnerability

    # Coverage check
    COVERAGE_CHECK = "coverage_check"  # Verify coverage collection works

    # Incremental build
    INC_BUILD_PULL = "inc_build_pull"  # Pull inc-build image from registry


# =============================================================================
# Benchmark Metadata Models
# =============================================================================


@dataclass
class Task:
    """Represents a benchmark task (delta or full mode).

    Delta mode: base_commit (clean) -> ref_commit (vulnerable)
    Full mode: base_commit (vulnerable), no ref_commit
    """

    mode: TaskMode
    base_commit: str
    ref_commit: Optional[str] = None  # Only for delta mode (vulnerable version)

    def __str__(self):
        if self.mode == TaskMode.DELTA:
            if self.ref_commit is None:
                raise ValueError("ref_commit must be set in DELTA mode")
            return f"Task(mode={self.mode.value}, base={self.base_commit[:8]}, ref={self.ref_commit[:8]})"
        return f"Task(mode={self.mode.value}, base={self.base_commit[:8]})"


# =============================================================================
# Job Context
# =============================================================================


@dataclass
class JobContext:
    """Context for a single test job.

    Uses simple fields instead of complex objects for harness/vulnerability/pov.
    Full metadata can be loaded via MetaYamlAdapter when needed.
    """

    job_type: ExecJobType
    task: Optional[Task]
    benchmark: str
    language: str
    engine: str
    sanitizer: str
    # Simple identifiers (use MetaYamlAdapter to get full details)
    harness_name: Optional[str] = None
    vuln_keyword: Optional[str] = None
    pov_id: Optional[str] = None
    # Computed paths (populated by runner using MetaYamlAdapter)
    patch_path: Optional[Path] = None
    pov_path: Optional[Path] = None
    use_inc_build: bool = False  # Use inc-build image (from project.yaml)

    def __str__(self):
        base = f"Job({self.job_type.value}, {self.benchmark}, {self.engine}/{self.sanitizer}"
        if self.harness_name:
            base += f", harness={self.harness_name}"
        if self.vuln_keyword:
            base += f", vuln={self.vuln_keyword}"
        if self.pov_id:
            base += f", pov={self.pov_id}"
        base += ")"
        return base

    def __lt__(self, other):
        """Sorting for consistent job ordering."""
        return (
            self.benchmark,
            self.job_type.value,
            self.engine,
            self.sanitizer,
            self.harness_name or "",
            self.vuln_keyword or "",
            self.pov_id or "",
        ) < (
            other.benchmark,
            other.job_type.value,
            other.engine,
            other.sanitizer,
            other.harness_name or "",
            other.vuln_keyword or "",
            other.pov_id or "",
        )


# =============================================================================
# Result Models
# =============================================================================


@dataclass
class IncBuildMetrics:
    """Metrics from incremental build testing.

    Stores timing and test count metrics for comparing baseline vs incremental builds.
    """

    # Build metrics
    build_time_baseline: Optional[float] = None  # seconds (without inc-build)
    build_time_inc: Optional[float] = None  # seconds (with inc-build)
    build_speedup: Optional[float] = None  # e.g., 7.93x

    # Test metrics
    test_time_baseline: Optional[float] = None  # seconds (without RTS)
    test_time_rts: Optional[float] = None  # seconds (with RTS)
    test_speedup: Optional[float] = None  # e.g., 3.60x

    # Test counts
    tests_total: Optional[int] = None  # total tests
    tests_selected: Optional[int] = None  # tests selected by RTS

    # RTS info
    rts_mode: Optional[str] = None  # "openclover", "binaryrts", etc.

    def compute_speedups(self) -> None:
        """Compute speedup values from baseline and inc-build times."""
        if self.build_time_baseline and self.build_time_inc and self.build_time_inc > 0:
            self.build_speedup = self.build_time_baseline / self.build_time_inc

        if self.test_time_baseline and self.test_time_rts and self.test_time_rts > 0:
            self.test_speedup = self.test_time_baseline / self.test_time_rts

    def __str__(self):
        parts = []
        if self.build_speedup is not None:
            parts.append(f"build_speedup={self.build_speedup:.2f}x")
        if self.test_speedup is not None:
            parts.append(f"test_speedup={self.test_speedup:.2f}x")
        return f"IncBuildMetrics({', '.join(parts)})" if parts else "IncBuildMetrics()"


@dataclass
class JobResult:
    """Result of a single job execution."""

    success: bool
    error: Optional[Exception] = None
    error_message: Optional[str] = None
    metrics: Optional[IncBuildMetrics] = None  # For inc-build jobs


@dataclass
class CIResult:
    """Result of a single CI job for reporting."""

    # Job identification
    benchmark: str
    job_type: str
    engine: str
    sanitizer: str

    # Result
    status: str  # "passed", "failed", "skipped"
    error_message: Optional[str] = None
    error_file: Optional[str] = None

    # Optional details
    harness: Optional[str] = None
    vulnerability: Optional[str] = None
    pov: Optional[str] = None

    # Timing
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_seconds: Optional[float] = None

    # Inc-build metrics (optional, only for INC_BUILD_TEST jobs)
    build_time_baseline: Optional[float] = None
    build_time_inc: Optional[float] = None
    build_speedup: Optional[float] = None
    test_time_baseline: Optional[float] = None
    test_time_rts: Optional[float] = None
    test_speedup: Optional[float] = None
    rts_mode: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV export."""
        return asdict(self)

    @classmethod
    def from_job_result(
        cls,
        job: JobContext,
        result: JobResult,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> "CIResult":
        """Create CIResult from JobContext and JobResult."""
        duration = None
        if start_time and end_time:
            duration = (end_time - start_time).total_seconds()

        ci_result = cls(
            benchmark=job.benchmark,
            job_type=job.job_type.value,
            engine=job.engine,
            sanitizer=job.sanitizer,
            status="passed" if result.success else "failed",
            error_message=result.error_message[:500] if result.error_message else None,
            harness=job.harness_name,
            vulnerability=job.vuln_keyword,
            pov=job.pov_id,
            start_time=start_time.isoformat() if start_time else None,
            end_time=end_time.isoformat() if end_time else None,
            duration_seconds=duration,
        )

        # Add inc-build metrics if present
        if result.metrics:
            ci_result.build_time_baseline = result.metrics.build_time_baseline
            ci_result.build_time_inc = result.metrics.build_time_inc
            ci_result.build_speedup = result.metrics.build_speedup
            ci_result.test_time_baseline = result.metrics.test_time_baseline
            ci_result.test_time_rts = result.metrics.test_time_rts
            ci_result.test_speedup = result.metrics.test_speedup
            ci_result.rts_mode = result.metrics.rts_mode

        return ci_result


# =============================================================================
# Result Collector
# =============================================================================


@dataclass
class ResultCollector:
    """Collects CI results and exports to CSV."""

    results: List[CIResult] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    def start(self) -> None:
        """Mark the start of CI run."""
        self.start_time = datetime.now()

    def finish(self) -> None:
        """Mark the end of CI run."""
        self.end_time = datetime.now()

    def add_result(self, result: CIResult) -> None:
        """Add a job result."""
        self.results.append(result)

    def add_skipped(self, benchmark: str, reason: str) -> CIResult:
        """Add a skipped benchmark result."""
        result = CIResult(
            benchmark=benchmark,
            job_type="skipped",
            engine="",
            sanitizer="",
            status="skipped",
            error_message=reason,
        )
        self.results.append(result)
        return result

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.status == "passed")
        failed = sum(1 for r in self.results if r.status == "failed")
        skipped = sum(1 for r in self.results if r.status == "skipped")

        # Group by benchmark
        by_benchmark: Dict[str, Dict[str, int]] = {}
        for r in self.results:
            if r.benchmark not in by_benchmark:
                by_benchmark[r.benchmark] = {"passed": 0, "failed": 0, "skipped": 0}
            by_benchmark[r.benchmark][r.status] += 1

        # Group by job type
        by_job_type: Dict[str, Dict[str, int]] = {}
        for r in self.results:
            if r.job_type not in by_job_type:
                by_job_type[r.job_type] = {"passed": 0, "failed": 0, "skipped": 0}
            by_job_type[r.job_type][r.status] += 1

        duration = None
        if self.start_time and self.end_time:
            duration = (self.end_time - self.start_time).total_seconds()

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": passed / total * 100 if total > 0 else 0,
            "duration_seconds": duration,
            "by_benchmark": by_benchmark,
            "by_job_type": by_job_type,
        }

    def export_csv(self, output_path: str) -> str:
        """Export results to CSV file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        columns = [
            "benchmark",
            "job_type",
            "engine",
            "sanitizer",
            "status",
            "harness",
            "vulnerability",
            "pov",
            "duration_seconds",
            "error_message",
            "error_file",
            "start_time",
            "end_time",
            # Inc-build metrics
            "build_time_baseline",
            "build_time_inc",
            "build_speedup",
            "test_time_baseline",
            "test_time_rts",
            "test_speedup",
            "rts_mode",
        ]

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for result in self.results:
                writer.writerow(result.to_dict())

        logger.info(f"Exported {len(self.results)} results to {path}")
        return str(path)

    def print_summary(self) -> None:
        """Print summary to console."""
        summary = self.get_summary()

        logger.info("=" * 80)
        logger.info("CI RESULTS SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Total jobs: {summary['total']}")
        logger.info(f"  Passed:  {summary['passed']}")
        logger.info(f"  Failed:  {summary['failed']}")
        logger.info(f"  Skipped: {summary['skipped']}")
        logger.info(f"  Pass rate: {summary['pass_rate']:.1f}%")

        if summary["duration_seconds"]:
            mins = summary["duration_seconds"] / 60
            logger.info(f"  Duration: {mins:.1f} minutes")

        # Print failed jobs
        failed_results = [r for r in self.results if r.status == "failed"]
        if failed_results:
            logger.info("-" * 80)
            logger.info(f"FAILED JOBS ({len(failed_results)}):")
            for i, r in enumerate(failed_results, 1):
                job_desc = f"{r.benchmark} | {r.job_type} | {r.engine}/{r.sanitizer}"
                if r.harness:
                    job_desc += f" | {r.harness}"
                if r.vulnerability:
                    job_desc += f" | {r.vulnerability}"
                logger.info(f"  {i}. {job_desc}")
                if r.error_file:
                    logger.info(f"     Error log: {r.error_file}")

        logger.info("=" * 80)


# =============================================================================
# Utility Functions
# =============================================================================


def get_benchmarks_root() -> str:
    """Get the path to benchmarks directory."""
    crsbench_root = Path(__file__).parent.parent.parent
    benchmarks_path = crsbench_root / "benchmarks"

    if not benchmarks_path.exists():
        raise RuntimeError(f"Benchmarks directory not found at {benchmarks_path}")

    return str(benchmarks_path)
