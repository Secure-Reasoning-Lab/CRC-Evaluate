"""Simplified data models for benchmark CI testing.

Simple result dataclasses for validation checks. No complex job types
or execution models - those are handled by existing engines.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class CheckStatus(Enum):
    """Status of a validation check."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass
class CheckResult:
    """Result of a single validation check."""

    status: CheckStatus
    time_seconds: float
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(cls, reason: str = "") -> "CheckResult":
        """Create a skipped result."""
        return cls(status=CheckStatus.SKIP, time_seconds=0.0, error=reason)

    @classmethod
    def make_error(cls, message: str, time_seconds: float = 0.0) -> "CheckResult":
        """Create an error result."""
        return cls(status=CheckStatus.ERROR, time_seconds=time_seconds, error=message)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status.value,
            "time_seconds": self.time_seconds,
            "error": self.error,
            "details": self.details,
        }


@dataclass
class BenchmarkValidationResult:
    """Complete validation results for a single benchmark."""

    benchmark: str
    benchmark_path: Path
    format_check: CheckResult
    pov_check: CheckResult
    patch_check: CheckResult
    coverage_check: Optional[CheckResult] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def total_status(self) -> CheckStatus:
        """Overall status - PASS only if all checks pass."""
        checks = [self.format_check, self.pov_check, self.patch_check]
        if self.coverage_check:
            checks.append(self.coverage_check)

        if any(c.status == CheckStatus.FAIL for c in checks):
            return CheckStatus.FAIL
        if any(c.status == CheckStatus.ERROR for c in checks):
            return CheckStatus.ERROR
        if all(c.status == CheckStatus.PASS for c in checks):
            return CheckStatus.PASS
        return CheckStatus.SKIP

    @property
    def total_time(self) -> float:
        """Total time for all checks."""
        total = (
            self.format_check.time_seconds
            + self.pov_check.time_seconds
            + self.patch_check.time_seconds
        )
        if self.coverage_check:
            total += self.coverage_check.time_seconds
        return total

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "benchmark": self.benchmark,
            "benchmark_path": str(self.benchmark_path),
            "total_status": self.total_status.value,
            "total_time_seconds": self.total_time,
            "format_check": self.format_check.to_dict(),
            "pov_check": self.pov_check.to_dict(),
            "patch_check": self.patch_check.to_dict(),
            "coverage_check": self.coverage_check.to_dict()
            if self.coverage_check
            else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass
class ValidationSummary:
    """Summary of validation results across multiple benchmarks."""

    results: list[BenchmarkValidationResult] = field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def total(self) -> int:
        """Total number of benchmarks."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Number of benchmarks that passed all checks."""
        return sum(1 for r in self.results if r.total_status == CheckStatus.PASS)

    @property
    def failed(self) -> int:
        """Number of benchmarks with failures."""
        return sum(1 for r in self.results if r.total_status == CheckStatus.FAIL)

    @property
    def errors(self) -> int:
        """Number of benchmarks with errors."""
        return sum(1 for r in self.results if r.total_status == CheckStatus.ERROR)

    def add_result(self, result: BenchmarkValidationResult) -> None:
        """Add a benchmark result."""
        self.results.append(result)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "errors": self.errors,
            },
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
