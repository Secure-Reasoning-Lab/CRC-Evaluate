"""Verification models for POV and patch validation.

This module defines the request/result data structures for verification.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class VerificationStatus(Enum):
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
class VerificationRequest:
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
class VerificationResult:
    """Result of POV verification.

    Attributes:
        status: Verification status (CPV, ZERODAY, NOT_VULNERABLE, etc.)
        benchmark: Name of the benchmark tested
        cpv_matched: List of CPV identifiers that this POV triggers
        pov_id: Optional identifier for the POV that was verified
        details: Optional additional details about the verification
        crash_info: Optional crash information (sanitizer output, etc.)
    """

    status: VerificationStatus
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
        return self.status in (VerificationStatus.CPV, VerificationStatus.ZERODAY)

    def __str__(self) -> str:
        if self.status == VerificationStatus.CPV:
            return (
                f"{self.benchmark}: {self.status.value} ({', '.join(self.cpv_matched)})"
            )
        return f"{self.benchmark}: {self.status.value}"
