"""Results collection and reporting for benchmark evaluations."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    from crsbench.evaluation.verification.models import VerificationResult


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

    def set_pov_stats(self, verification_results: List["VerificationResult"]) -> None:
        """Set POV statistics from verification results.

        Args:
            verification_results: List of VerificationResult objects from validation module

        Note:
            Maps VerificationStatus to POV statistics:
            - CPV, ZERODAY: POV found (triggers a vulnerability)
            - NOT_VULNERABLE, UNINTENDED_CRASH: POV missed (doesn't trigger expected vuln)
            - ERROR: Error during verification
        """
        from crsbench.evaluation.verification.models import VerificationStatus

        self.total_povs = len(
            verification_results
        )  # TODO: check total_povs; should from ground truth
        self.povs_found = sum(
            1
            for r in verification_results
            if r.status in (VerificationStatus.CPV, VerificationStatus.ZERODAY)
        )
        self.povs_missed = sum(
            1
            for r in verification_results
            if r.status
            in (VerificationStatus.NOT_VULNERABLE, VerificationStatus.UNINTENDED_CRASH)
        )
        self.povs_error = sum(
            1 for r in verification_results if r.status == VerificationStatus.ERROR
        )

    def set_patch_stats(self, total_input_povs: int, patches: Dict[str, str]) -> None:
        """Set patch statistics from collected patches.

        Args:
            total_input_povs: Number of POVs provided to the patch CRS
            patches: Dict mapping POV ID to patch content
        """
        self.total_input_povs = total_input_povs
        self.patches_generated = len(patches)

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
            base_commit=self.base_commit,
            ref_commit=self.ref_commit,
            crs_config=self.crs_config,
        )
