"""Result collection and CSV export for benchmark CI testing."""

import csv
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from crsbench.benchmark_ci.utils import JobContext, ExecJobType
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CIResult:
    """Result of a single CI job execution."""

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

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV export."""
        return asdict(self)


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

    def add_result(
        self,
        job: JobContext,
        status: str,
        error: Optional[Exception] = None,
        error_file: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> CIResult:
        """Add a job result.

        Args:
            job: Job context
            status: "passed", "failed", or "skipped"
            error: Exception if failed
            error_file: Path to error log file
            start_time: Job start time
            end_time: Job end time

        Returns:
            Created CIResult
        """
        duration = None
        if start_time and end_time:
            duration = (end_time - start_time).total_seconds()

        result = CIResult(
            benchmark=job.benchmark,
            job_type=job.job_type.value,
            engine=job.engine,
            sanitizer=job.sanitizer,
            status=status,
            error_message=str(error)[:500] if error else None,
            error_file=error_file,
            harness=job.harness.name if job.harness else None,
            vulnerability=job.vulnerability.id if job.vulnerability else None,
            pov=job.pov.id if job.pov else None,
            start_time=start_time.isoformat() if start_time else None,
            end_time=end_time.isoformat() if end_time else None,
            duration_seconds=duration,
        )

        self.results.append(result)
        return result

    def add_skipped(self, benchmark: str, reason: str) -> CIResult:
        """Add a skipped benchmark result.

        Args:
            benchmark: Benchmark name
            reason: Reason for skipping

        Returns:
            Created CIResult
        """
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
        """Get summary statistics.

        Returns:
            Dictionary with summary stats
        """
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
        """Export results to CSV file.

        Args:
            output_path: Path to output CSV file

        Returns:
            Path to created CSV file
        """
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Define CSV columns
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
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()

            for result in self.results:
                writer.writerow(result.to_dict())

        logger.info(f"Exported {len(self.results)} results to {path}")
        return str(path)

    def export_summary_csv(self, output_path: str) -> str:
        """Export summary statistics to CSV file.

        Args:
            output_path: Path to output CSV file

        Returns:
            Path to created CSV file
        """
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        summary = self.get_summary()

        # Write benchmark summary
        columns = ["benchmark", "passed", "failed", "skipped", "total", "pass_rate"]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()

            for benchmark, stats in summary["by_benchmark"].items():
                total = stats["passed"] + stats["failed"] + stats["skipped"]
                pass_rate = stats["passed"] / total * 100 if total > 0 else 0
                writer.writerow({
                    "benchmark": benchmark,
                    "passed": stats["passed"],
                    "failed": stats["failed"],
                    "skipped": stats["skipped"],
                    "total": total,
                    "pass_rate": f"{pass_rate:.1f}%",
                })

            # Write total row
            writer.writerow({
                "benchmark": "TOTAL",
                "passed": summary["passed"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
                "total": summary["total"],
                "pass_rate": f"{summary['pass_rate']:.1f}%",
            })

        logger.info(f"Exported summary to {path}")
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
