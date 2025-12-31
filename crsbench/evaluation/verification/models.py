"""Verification models for POV and patch verification.

This module defines the request/result data structures for both
POV verification (bug finding) and patch verification (bug fixing).
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

# =============================================================================
# POV Verification Models
# =============================================================================


class PovVerificationStatus(Enum):
    """Result status of POV verification.

    - NOT_VULNERABLE: POV does not trigger any vulnerability
    - CPV: POV triggers one or more known CPVs
    - ZERODAY: POV triggers a crash not covered by any known CPV
    - UNINTENDED_CRASH: POV crashes even with all patches applied
    - ERROR: Verification failed due to an error
    """

    NOT_VULNERABLE = "not_vulnerable"
    CPV = "cpv"
    ZERODAY = "zeroday"
    UNINTENDED_CRASH = "unintended_crash"
    ERROR = "error"


@dataclass
class PovVerificationRequest:
    """Request to verify a POV against a benchmark.

    Attributes:
        pov_data: Raw bytes of the POV/testcase
        harness: Name of the fuzz harness to run
        benchmark: Optional specific benchmark to test against
        pov_id: Optional identifier for this POV
    """

    pov_data: bytes
    harness: str
    benchmark: Optional[str] = None
    pov_id: Optional[str] = None


@dataclass
class PovVerificationResult:
    """Result of POV verification.

    Attributes:
        status: Verification status (CPV, ZERODAY, NOT_VULNERABLE, etc.)
        benchmark: Name of the benchmark tested
        cpv_matched: List of CPV identifiers that this POV triggers
        pov_id: Optional identifier for the POV that was verified
        details: Optional additional details about the verification
        crash_info: Optional crash information (sanitizer output, etc.)
    """

    status: PovVerificationStatus
    benchmark: str
    cpv_matched: list[str] = field(default_factory=list)
    pov_id: Optional[str] = None
    details: Optional[str] = None
    crash_info: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for serialization."""
        result: dict[str, Any] = {
            "status": self.status.value,
            "benchmark": self.benchmark,
            "cpv_matched": self.cpv_matched,
        }
        if self.pov_id:
            result["pov_id"] = self.pov_id
        if self.details:
            result["details"] = self.details
        if self.crash_info:
            result["crash_info"] = self.crash_info
        return result

    @property
    def is_vulnerability(self) -> bool:
        """Return True if the POV triggers any vulnerability."""
        return self.status in (
            PovVerificationStatus.CPV,
            PovVerificationStatus.ZERODAY,
        )

    def __str__(self) -> str:
        if self.status == PovVerificationStatus.CPV:
            return (
                f"{self.benchmark}: {self.status.value} ({', '.join(self.cpv_matched)})"
            )
        return f"{self.benchmark}: {self.status.value}"


# =============================================================================
# Patch Verification Models
# =============================================================================


class PatchVerificationStatus(Enum):
    """Result status of patch verification.

    - PENDING: Verification not yet started
    - BUILD_FAILED: Patch could not be applied or build failed
    - POV_STILL_TRIGGERS: Original POV still crashes after patch
    - TEST_FAILED: Unit tests failed
    - VALID: Patch is valid (fixes bug without breaking tests)
    - ERROR: Verification failed due to an error
    """

    PENDING = "pending"
    BUILD_FAILED = "build_failed"
    POV_STILL_TRIGGERS = "pov_still_triggers"
    TEST_FAILED = "test_failed"
    VALID = "valid"
    ERROR = "error"


type CpvStatus = Literal["complete", "partial", "none"]
type SecurityVerdict = Literal["PASS", "FAIL"]


@dataclass
class CpvStats:
    """Per-CPV statistics for verification results.

    Tracks how many POV variants were tested and matched for a single CPV,
    providing detailed breakdown of patch effectiveness.

    Attributes:
        cpv_id: CPV identifier (e.g., "cpv-1", "cpv-2")
        variants_tested: Number of POV variants tested for this CPV
        variants_matched: Number of variants fixed by the patch
        variant_results: Map of variant_id -> whether it was fixed
    """

    cpv_id: str
    variants_tested: int = 0
    variants_matched: int = 0
    variant_results: dict[str, bool] = field(default_factory=dict)

    @property
    def status(self) -> CpvStatus:
        """Return the fix status for this CPV.

        Returns:
            "complete" if all variants fixed, "partial" if some, "none" if zero
        """
        if self.variants_tested == 0:
            return "none"
        if self.variants_matched == self.variants_tested:
            return "complete"
        if self.variants_matched > 0:
            return "partial"
        return "none"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "cpv_id": self.cpv_id,
            "variants_tested": self.variants_tested,
            "variants_matched": self.variants_matched,
            "variant_results": self.variant_results,
            "status": self.status,
        }


@dataclass
class VerificationScores:
    """Aggregate benchmark scoring metrics for patch verification.

    Provides summary statistics across all CPVs tested, useful for
    comparing patch quality across different CRS implementations.

    Attributes:
        cpvs_complete: Number of CPVs fully fixed (all variants pass)
        cpvs_partial: Number of CPVs partially fixed (some variants pass)
        cpvs_none: Number of CPVs not fixed at all
        total_variants_tested: Total POV variants tested across all CPVs
        total_variants_matched: Total POV variants fixed across all CPVs
    """

    cpvs_complete: int = 0
    cpvs_partial: int = 0
    cpvs_none: int = 0
    total_variants_tested: int = 0
    total_variants_matched: int = 0

    @property
    def overall_fix_rate(self) -> float:
        """Return the overall fix rate as a ratio.

        Returns:
            Ratio of variants_matched / variants_tested, or 0.0 if none tested
        """
        if self.total_variants_tested == 0:
            return 0.0
        return self.total_variants_matched / self.total_variants_tested

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "cpvs_complete": self.cpvs_complete,
            "cpvs_partial": self.cpvs_partial,
            "cpvs_none": self.cpvs_none,
            "total_variants_tested": self.total_variants_tested,
            "total_variants_matched": self.total_variants_matched,
            "overall_fix_rate": self.overall_fix_rate,
        }


class TestMode(Enum):
    """Unit test execution mode.

    - FULL: Run complete test suite
    - RTS: Run only patch-affected tests (Regression Test Selection)
    """

    FULL = "full"
    RTS = "rts"


@dataclass
class PatchInfo:
    """Information about a patch to verify.

    Attributes:
        patch_id: Unique patch identifier (e.g., "patch_0")
        pov_id: POV/CPV identifier this patch targets (e.g., "pov_0", "cpv_0")
        patch_path: Path to the patch file
        patch_content: Content of the patch (unified diff format)
    """

    patch_id: str
    pov_id: str
    patch_path: Path
    patch_content: str = ""

    def __post_init__(self) -> None:
        """Load patch content if not provided."""
        if not self.patch_content and self.patch_path.exists():
            self.patch_content = self.patch_path.read_text()


@dataclass
class PatchVerificationResult:
    """Result of patch verification.

    Attributes:
        status: Verification status
        patch_id: Unique patch identifier (e.g., "patch_0")
        pov_id: POV/CPV identifier this patch targets (e.g., "pov_0", "cpv_0")
        benchmark: Benchmark name (e.g., "sanity-mock-c-delta-01")
        patch_path: Path to the verified patch
        harness: Name of the harness tested
        details: Optional details about the verification
        elapsed_seconds: Time taken for build and verification (seconds)
        pov_test_passed: Whether the POV test passed (no crash)
        unit_tests_passed: Whether unit tests passed (None if not run)
        unit_tests_run: Number of unit tests run
        unit_tests_failed: Number of unit tests failed
        failed_tests: List of failed test names
        cpv_fixed: List of fully fixed CPV identifiers (all variants pass)
        cpv_stats: Per-CPV breakdown of verification results
        scores: Aggregate benchmark scoring metrics
        security_verdict: Binary security verdict (PASS if all CPVs fixed)
    """

    status: PatchVerificationStatus
    patch_id: str
    pov_id: str
    benchmark: str
    patch_path: Path
    harness: str = ""
    details: Optional[str] = None
    elapsed_seconds: float = 0.0
    pov_test_passed: bool = False
    unit_tests_passed: Optional[bool] = None
    unit_tests_run: int = 0
    unit_tests_failed: int = 0
    failed_tests: list[str] = field(default_factory=list)
    cpv_fixed: list[str] = field(default_factory=list)
    cpv_stats: dict[str, CpvStats] = field(default_factory=dict)
    scores: Optional[VerificationScores] = None
    security_verdict: SecurityVerdict = "FAIL"

    @property
    def is_valid(self) -> bool:
        """Return True if the patch is valid."""
        return self.status == PatchVerificationStatus.VALID

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for serialization."""
        result: dict[str, Any] = {
            "status": self.status.value,
            "patch_id": self.patch_id,
            "pov_id": self.pov_id,
            "benchmark": self.benchmark,
            "patch_path": str(self.patch_path),
            "harness": self.harness,
            "details": self.details,
            "elapsed_seconds": self.elapsed_seconds,
            "pov_test_passed": self.pov_test_passed,
            "unit_tests_passed": self.unit_tests_passed,
            "unit_tests_run": self.unit_tests_run,
            "unit_tests_failed": self.unit_tests_failed,
            "failed_tests": self.failed_tests,
            "cpv_fixed": self.cpv_fixed,
            "cpv_stats": {
                cpv_id: stats.to_dict() for cpv_id, stats in self.cpv_stats.items()
            },
            "security_verdict": self.security_verdict,
        }
        if self.scores is not None:
            result["scores"] = self.scores.to_dict()
        return result

    def __str__(self) -> str:
        status_str = self.status.value.upper()
        if self.is_valid:
            return f"{self.patch_id} ({self.pov_id}): {status_str}"
        return f"{self.patch_id} ({self.pov_id}): {status_str} - {self.details or 'Unknown error'}"
