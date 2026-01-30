"""Results collection and reporting for benchmark evaluations."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from crsbench.evaluation.runner import EvaluationResult
    from crsbench.evaluation.verification.models import (
        PatchVerificationResult,
        PovVerificationResult,
    )


@dataclass
class HarnessResult:
    """Result for a single harness file."""

    name: str
    path: str
    execution_time: Optional[float] = None
    run_successful: bool = True
    run_output: Optional[str] = None


@dataclass
class EvaluationReport:
    """Complete evaluation report for a benchmark."""

    benchmark_path: str
    evaluation_mode: str  # "delta" or "full"
    start_time: datetime
    end_time: datetime
    total_execution_time: float
    harness_results: List[HarnessResult]

    # Summary statistics (POV - for bug-finding CRS)
    total_povs: int
    povs_found: int
    povs_missed: int
    povs_error: int

    # Summary statistics (Patch - for bug-fixing CRS)
    total_input_povs: int = 0  # Number of POVs given to patch CRS
    patches_generated: int = 0  # Number of patches generated

    # Patch verification results
    patches_valid: int = 0  # VALID - patch fixes vulnerability and passes tests
    patches_build_failed: int = 0  # BUILD_FAILED - patch failed to build
    patches_pov_triggers: int = 0  # POV_STILL_TRIGGERS - vulnerability not fixed
    patches_test_failed: int = 0  # TEST_FAILED - unit tests failed
    patches_error: int = 0  # ERROR - verification error

    # Configuration info
    base_commit: Optional[str] = None
    ref_commit: Optional[str] = None
    crs_config: Optional[Dict[str, Any]] = None

    @property
    def success_rate(self) -> float:
        """Calculate POV detection success rate."""
        total_attempted = self.povs_found + self.povs_missed
        if total_attempted == 0:
            return 0.0
        return self.povs_found / total_attempted

    @property
    def error_rate(self) -> float:
        """Calculate POV execution error rate."""
        if self.total_povs == 0:
            return 0.0
        return self.povs_error / self.total_povs

    @property
    def success(self) -> bool:
        """Return True if all harness runs were successful."""
        return all(hr.run_successful for hr in self.harness_results)

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary."""
        data = asdict(self)
        # Convert datetime objects to strings
        data["start_time"] = self.start_time.isoformat()
        data["end_time"] = self.end_time.isoformat()
        return data

    def to_json(self, indent: int = 2) -> str:
        """Convert report to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def to_yaml(self) -> str:
        """Convert report to YAML string."""
        return yaml.dump(self.to_dict(), default_flow_style=False, indent=2)

    def save_json(self, path: Path) -> None:
        """Save report as JSON file."""
        with path.open("w", encoding="utf-8") as f:
            f.write(self.to_json())

    def save_yaml(self, path: Path) -> None:
        """Save report as YAML file."""
        with path.open("w", encoding="utf-8") as f:
            f.write(self.to_yaml())


class ResultCollector:
    """Collects and manages evaluation results."""

    def __init__(self, benchmark_path: str, evaluation_mode: str):
        self.benchmark_path = benchmark_path
        self.evaluation_mode = evaluation_mode
        self.start_time = datetime.now()
        self.harness_results: List[HarnessResult] = []
        self.base_commit: Optional[str] = None
        self.ref_commit: Optional[str] = None
        self.crs_config: Optional[Dict[str, Any]] = None
        # POV statistics (set by verification, for bug-finding CRS)
        self.total_povs = 0
        self.povs_found = 0
        self.povs_missed = 0
        self.povs_error = 0
        # Patch statistics (for bug-fixing CRS)
        self.total_input_povs = 0
        self.patches_generated = 0
        # Patch verification statistics
        self.patches_valid = 0
        self.patches_build_failed = 0
        self.patches_pov_triggers = 0
        self.patches_test_failed = 0
        self.patches_error = 0

    def add_harness_result(self, harness_result: HarnessResult) -> None:
        """Add result for a harness."""
        self.harness_results.append(harness_result)

    def set_commits(self, base_commit: str, ref_commit: Optional[str] = None) -> None:
        """Set git commit information."""
        self.base_commit = base_commit
        self.ref_commit = ref_commit

    def set_crs_config(self, config: Dict[str, Any]) -> None:
        """Set CRS configuration."""
        self.crs_config = config

    def set_pov_stats(
        self, verification_results: List["PovVerificationResult"]
    ) -> None:
        """Set POV statistics from verification results.

        Args:
            verification_results: List of PovVerificationResult objects from validation module

        Note:
            Maps PovVerificationStatus to POV statistics:
            - CPV: POV found (triggers a known vulnerability)
            - NOT_VULNERABLE, UNINTENDED_CRASH: POV missed (doesn't trigger expected vuln)
            - ERROR: Error during verification
        """
        from crsbench.evaluation.verification.models import PovVerificationStatus

        self.total_povs = len(
            verification_results
        )  # TODO: check total_povs; should from ground truth
        self.povs_found = sum(
            1 for r in verification_results if r.status == PovVerificationStatus.CPV
        )
        self.povs_missed = sum(
            1
            for r in verification_results
            if r.status
            in (
                PovVerificationStatus.NOT_VULNERABLE,
                PovVerificationStatus.UNINTENDED_CRASH,
            )
        )
        self.povs_error = sum(
            1 for r in verification_results if r.status == PovVerificationStatus.ERROR
        )

    def set_patch_stats(
        self, total_input_povs: int, results: List["PatchVerificationResult"]
    ) -> None:
        """Set patch statistics from verification results.

        Similar to set_pov_stats(), this derives all patch statistics from
        verification results.

        Args:
            total_input_povs: Number of POVs provided to the patch CRS
            results: List of PatchVerificationResult objects

        Note:
            Maps PatchVerificationStatus to statistics:
            - VALID: Patch fixes vulnerability and passes tests
            - BUILD_FAILED: Patch failed to build
            - POV_STILL_TRIGGERS: Vulnerability not fixed by patch
            - TEST_FAILED: Unit tests failed after patching
            - ERROR, PENDING: Verification errors
        """
        from crsbench.evaluation.verification.models import PatchVerificationStatus

        self.total_input_povs = total_input_povs
        self.patches_generated = len(results)

        self.patches_valid = sum(
            1 for r in results if r.status == PatchVerificationStatus.VALID
        )
        self.patches_build_failed = sum(
            1 for r in results if r.status == PatchVerificationStatus.BUILD_FAILED
        )
        self.patches_pov_triggers = sum(
            1 for r in results if r.status == PatchVerificationStatus.POV_STILL_TRIGGERS
        )
        self.patches_test_failed = sum(
            1 for r in results if r.status == PatchVerificationStatus.TEST_FAILED
        )
        self.patches_error = sum(
            1
            for r in results
            if r.status
            in (PatchVerificationStatus.ERROR, PatchVerificationStatus.PENDING)
        )

    def finalize_report(self) -> EvaluationReport:
        """Create final evaluation report."""
        end_time = datetime.now()
        total_execution_time = (end_time - self.start_time).total_seconds()

        return EvaluationReport(
            benchmark_path=self.benchmark_path,
            evaluation_mode=self.evaluation_mode,
            start_time=self.start_time,
            end_time=end_time,
            total_execution_time=total_execution_time,
            harness_results=self.harness_results,
            total_povs=self.total_povs,
            povs_found=self.povs_found,
            povs_missed=self.povs_missed,
            povs_error=self.povs_error,
            total_input_povs=self.total_input_povs,
            patches_generated=self.patches_generated,
            patches_valid=self.patches_valid,
            patches_build_failed=self.patches_build_failed,
            patches_pov_triggers=self.patches_pov_triggers,
            patches_test_failed=self.patches_test_failed,
            patches_error=self.patches_error,
            base_commit=self.base_commit,
            ref_commit=self.ref_commit,
            crs_config=self.crs_config,
        )


class CRSType(str, Enum):
    """Type of CRS being evaluated."""

    BUG_FINDING = "bug-finding"
    BUG_FIXING = "bug-fixing"


class TrialMetadata(BaseModel):
    """Metadata for a trial execution."""

    model_config = ConfigDict(extra="allow")

    experiment_filestore: Optional[str] = None
    max_total_time: Optional[int] = None
    difficulty_level: Optional[int] = None
    timestamp_start: float
    timestamp_end: float


class TrialResult(BaseModel):
    """Result from a single trial execution.

    This model captures the outcome of running a CRS on a benchmark/harness
    combination for a single trial. Supports both bug-finding and bug-fixing CRS types.
    """

    model_config = ConfigDict(extra="allow")

    # Core identifiers
    crs: str
    benchmark: str
    harness: str
    trial_num: int
    crs_type: CRSType

    # Execution status
    success: bool
    execution_time: float
    error: Optional[str] = None
    error_type: Optional[str] = None

    # Bug-finding CRS metrics
    povs_found: int = 0
    total_povs: int = 0

    # Bug-fixing CRS metrics
    patches_generated: int = 0
    patches_valid: int = 0

    # Full report (as dict for flexibility)
    report: Dict[str, Any]

    # Metadata
    metadata: TrialMetadata

    @property
    def success_rate(self) -> float:
        """POV detection success rate (for bug-finding CRS)."""
        if self.total_povs == 0:
            return 0.0
        return self.povs_found / self.total_povs

    @property
    def patch_valid_rate(self) -> float:
        """Patch validation rate (for bug-fixing CRS)."""
        if self.patches_generated == 0:
            return 0.0
        return self.patches_valid / self.patches_generated

    def log_summary(self) -> str:
        """Generate a summary log message based on CRS type."""
        base = (
            f"[Trial {self.trial_num}] Completed {self.crs} on "
            f"{self.benchmark}/{self.harness}: "
        )
        if self.crs_type == CRSType.BUG_FIXING:
            return (
                f"{base}{self.patches_generated} patches generated, "
                f"{self.patches_valid} valid in {self.execution_time:.1f}s"
            )
        return (
            f"{base}{self.povs_found}/{self.total_povs} POVs found "
            f"({self.success_rate:.1%}) in {self.execution_time:.1f}s"
        )

    @classmethod
    def from_evaluation(
        cls,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        crs_type: CRSType,
        result: "EvaluationResult",
        execution_time: float,
        metadata: TrialMetadata,
    ) -> "TrialResult":
        """Create TrialResult from an EvaluationResult.

        Args:
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            crs_type: Type of CRS (bug-finding or bug-fixing)
            result: EvaluationResult from runner
            execution_time: Total execution time in seconds
            metadata: Trial metadata

        Returns:
            TrialResult instance
        """
        return cls(
            crs=crs,
            benchmark=benchmark,
            harness=harness,
            trial_num=trial_num,
            crs_type=crs_type,
            success=True,  # Validation passed (exception raised if invalid)
            execution_time=execution_time,
            povs_found=result.povs_found,
            total_povs=result.total_povs,
            patches_generated=result.report.patches_generated,
            patches_valid=result.report.patches_valid,
            report=result.report.to_dict(),
            metadata=metadata,
        )
