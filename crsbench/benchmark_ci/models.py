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


class CheckMode(Enum):
    """Mode for CI validation checks.

    Controls which build/verification variants are tested.
    """

    FORMAT = "format"  # Format validation only (Fmt column)
    DEFAULT = "default"  # Standard checks only (POV, Patch)
    INC = "inc"  # Inc-build variants only (POV:inc, Patch:inc)
    RTS = "rts"  # RTS variants only (Patch:rts)
    INC_RTS = (
        "inc-rts"  # Inc + RTS + combined (POV:inc, Patch:inc, Patch:rts, Patch:inc-rts)
    )
    ALL = "all"  # Full matrix (all variants)


@dataclass
class CheckResult:
    """Result of a single validation check."""

    status: CheckStatus
    time_seconds: float
    build_time: float = 0.0
    verify_time: float = 0.0
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    fallback_used: bool = False  # True if inc-build fell back to standard build

    @classmethod
    def skip(cls, reason: str = "") -> "CheckResult":
        """Create a skipped result."""
        return cls(status=CheckStatus.SKIP, time_seconds=0.0, error=reason)

    @classmethod
    def make_error(cls, message: str, time_seconds: float = 0.0) -> "CheckResult":
        """Create an error result."""
        return cls(status=CheckStatus.ERROR, time_seconds=time_seconds, error=message)

    def format_status(self) -> str:
        """Format status for display with time and fallback indicator.

        Returns:
            Formatted string like "PASS(2m)", "PASS-FB(8m)", "FAIL", "-"
        """
        if self.status == CheckStatus.SKIP:
            return "-"
        if self.status == CheckStatus.FAIL:
            return "FAIL"
        if self.status == CheckStatus.ERROR:
            return "ERROR"

        # Use the most specific time available
        if self.verify_time > 0:
            time_str = _format_time_short(self.verify_time)
        elif self.build_time > 0:
            time_str = _format_time_short(self.build_time)
        else:
            time_str = _format_time_short(self.time_seconds)

        # PASS with optional fallback indicator
        if self.fallback_used:
            return f"PASS-FB({time_str})"
        return f"PASS({time_str})"

    def format_var_status(self) -> str:
        """Format variant result for display as X/Y(time).

        Returns:
            Formatted string like "6/18(5s)", "SKIP", "ERR"
        """
        if self.status == CheckStatus.SKIP:
            return "SKIP"
        if self.status == CheckStatus.ERROR:
            return "ERR"

        var_passed = self.details.get("var_passed", 0)
        var_total = self.details.get("var_total", 0)

        if var_total == 0:
            return "SKIP"

        # Include time if available
        time_val = self.verify_time if self.verify_time > 0 else self.time_seconds
        if time_val > 0:
            time_str = _format_time_short(time_val)
            return f"{var_passed}/{var_total}({time_str})"

        return f"{var_passed}/{var_total}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status.value,
            "time_seconds": self.time_seconds,
            "build_time": self.build_time,
            "verify_time": self.verify_time,
            "error": self.error,
            "details": self.details,
            "fallback_used": self.fallback_used,
        }


def _format_time_short(seconds: float) -> str:
    """Format time in short form (e.g., '2m', '30s', '1h')."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m"
    hours = seconds / 3600
    return f"{hours:.1f}h"


@dataclass
class BenchmarkValidationResult:
    """Complete validation results for a single benchmark."""

    benchmark: str
    benchmark_path: Path
    format_check: Optional[CheckResult] = None
    # Per-check format sub-results (struct, schema, harness, cpv, blob, patch)
    format_checks: dict[str, CheckResult] = field(default_factory=dict)
    pov_check: Optional[CheckResult] = None  # Combined result (legacy)
    # Split POV results (build, pov_0 verify, variants)
    pov_build_check: Optional[CheckResult] = None
    pov_pov_check: Optional[CheckResult] = None  # Ground truth pov_0 only
    pov_var_check: Optional[CheckResult] = None  # Variant POVs (pov_1+)
    patch_check: Optional[CheckResult] = None  # Combined result (legacy)
    coverage_check: Optional[CheckResult] = None
    # Split patch results (build, pov_0 test, variants, unit test)
    patch_build_check: Optional[CheckResult] = None
    patch_pov_check: Optional[CheckResult] = None  # Ground truth pov_0 only
    patch_var_check: Optional[CheckResult] = None  # Variant POVs (pov_1+)
    patch_unittest_check: Optional[CheckResult] = None
    # Variant-specific results (inc-build, RTS)
    pov_inc_check: Optional[CheckResult] = None
    patch_inc_check: Optional[CheckResult] = None
    patch_rts_check: Optional[CheckResult] = None
    patch_inc_rts_check: Optional[CheckResult] = None
    coverage_inc_check: Optional[CheckResult] = None
    # Shared build time (BuildVariantsJob — one per benchmark)
    shared_build_time: float = 0.0
    # Storage metrics (total bytes for build artifacts, Docker, git)
    storage_bytes: int = 0
    # Benchmark capabilities from project.yaml
    supports_inc_build: bool = True
    rts_mode: Optional[str] = None  # none, jcgeks, openclover, binaryrts
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def total_status(self) -> CheckStatus:
        """Overall status - PASS if all executed checks pass.

        None checks (not requested) are ignored entirely.
        SKIP checks are ignored when determining pass/fail status.
        Returns PASS if all non-SKIP checks passed, SKIP if all checks were skipped.
        """
        all_checks = [
            self.format_check,
            self.pov_check,
            self.patch_check,
            self.coverage_check,
            self.pov_inc_check,
            self.patch_inc_check,
            self.patch_rts_check,
            self.patch_inc_rts_check,
            self.coverage_inc_check,
        ]
        # Filter out None (not requested by command)
        checks = [c for c in all_checks if c is not None]
        if not checks:
            return CheckStatus.SKIP

        # Check for failures and errors first
        if any(c.status == CheckStatus.FAIL for c in checks):
            return CheckStatus.FAIL
        if any(c.status == CheckStatus.ERROR for c in checks):
            return CheckStatus.ERROR

        # Filter out SKIP checks to determine if all executed checks passed
        executed_checks = [c for c in checks if c.status != CheckStatus.SKIP]
        if not executed_checks:
            return CheckStatus.SKIP
        if all(c.status == CheckStatus.PASS for c in executed_checks):
            return CheckStatus.PASS
        return CheckStatus.SKIP

    @property
    def total_time(self) -> float:
        """Total time for all checks."""
        all_checks = [
            self.format_check,
            self.pov_check,
            self.patch_check,
            self.coverage_check,
            self.pov_inc_check,
            self.patch_inc_check,
            self.patch_rts_check,
            self.patch_inc_rts_check,
            self.coverage_inc_check,
        ]
        return sum(c.time_seconds for c in all_checks if c is not None)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "benchmark": self.benchmark,
            "benchmark_path": str(self.benchmark_path),
            "total_status": self.total_status.value,
            "total_time_seconds": self.total_time,
            "format_check": self.format_check.to_dict() if self.format_check else None,
            "format_checks": {k: v.to_dict() for k, v in self.format_checks.items()},
            "pov_check": self.pov_check.to_dict() if self.pov_check else None,
            "pov_build_check": self.pov_build_check.to_dict()
            if self.pov_build_check
            else None,
            "pov_pov_check": self.pov_pov_check.to_dict()
            if self.pov_pov_check
            else None,
            "pov_var_check": self.pov_var_check.to_dict()
            if self.pov_var_check
            else None,
            "patch_check": self.patch_check.to_dict() if self.patch_check else None,
            "patch_build_check": self.patch_build_check.to_dict()
            if self.patch_build_check
            else None,
            "patch_pov_check": self.patch_pov_check.to_dict()
            if self.patch_pov_check
            else None,
            "patch_var_check": self.patch_var_check.to_dict()
            if self.patch_var_check
            else None,
            "patch_unittest_check": self.patch_unittest_check.to_dict()
            if self.patch_unittest_check
            else None,
            "coverage_check": self.coverage_check.to_dict()
            if self.coverage_check
            else None,
            "pov_inc_check": self.pov_inc_check.to_dict()
            if self.pov_inc_check
            else None,
            "patch_inc_check": self.patch_inc_check.to_dict()
            if self.patch_inc_check
            else None,
            "patch_rts_check": self.patch_rts_check.to_dict()
            if self.patch_rts_check
            else None,
            "patch_inc_rts_check": self.patch_inc_rts_check.to_dict()
            if self.patch_inc_rts_check
            else None,
            "coverage_inc_check": self.coverage_inc_check.to_dict()
            if self.coverage_inc_check
            else None,
            "shared_build_time": self.shared_build_time,
            "storage_bytes": self.storage_bytes,
            "supports_inc_build": self.supports_inc_build,
            "rts_mode": self.rts_mode,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass
class ValidationSummary:
    """Summary of validation results across multiple benchmarks."""

    results: list[BenchmarkValidationResult] = field(default_factory=list)
    check_mode: CheckMode = CheckMode.DEFAULT
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
            "check_mode": self.check_mode.value,
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
