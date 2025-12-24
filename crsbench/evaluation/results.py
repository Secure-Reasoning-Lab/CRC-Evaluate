"""Results collection and reporting for benchmark evaluations."""

import json
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum


class POVStatus(Enum):
    """Status of a POV detection."""
    NOT_RUN = "not_run"
    FOUND = "found"
    MISSED = "missed"
    ERROR = "error"


@dataclass
class POVResult:
    """Result for a single POV."""
    name: str
    harness_name: str
    sanitizer: str
    error_token: Optional[str]
    status: POVStatus
    execution_time: Optional[float] = None
    error_message: Optional[str] = None
    crs_output: Optional[str] = None


@dataclass
class HarnessResult:
    """Result for a single harness file."""
    name: str
    path: str
    pov_results: List[POVResult]
    execution_time: Optional[float] = None
    build_successful: bool = True
    build_output: Optional[str] = None


@dataclass
class EvaluationReport:
    """Complete evaluation report for a benchmark."""
    benchmark_path: str
    evaluation_mode: str  # "delta" or "full"
    start_time: datetime
    end_time: datetime
    total_execution_time: float
    harness_results: List[HarnessResult]

    # Summary statistics
    total_povs: int
    povs_found: int
    povs_missed: int
    povs_error: int

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
        data['start_time'] = self.start_time.isoformat()
        data['end_time'] = self.end_time.isoformat()
        # Convert enums to strings
        for harness_result in data['harness_results']:
            for pov_result in harness_result['pov_results']:
                pov_result['status'] = pov_result['status'].value
        return data

    def to_json(self, indent: int = 2) -> str:
        """Convert report to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def to_yaml(self) -> str:
        """Convert report to YAML string."""
        return yaml.dump(self.to_dict(), default_flow_style=False, indent=2)

    def save_json(self, path: Path) -> None:
        """Save report as JSON file."""
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.to_json())

    def save_yaml(self, path: Path) -> None:
        """Save report as YAML file."""
        with open(path, 'w', encoding='utf-8') as f:
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

    def finalize_report(self) -> EvaluationReport:
        """Create final evaluation report."""
        end_time = datetime.now()
        total_execution_time = (end_time - self.start_time).total_seconds()

        # Calculate summary statistics
        total_povs = 0
        povs_found = 0
        povs_missed = 0
        povs_error = 0

        # FIXME: no pov found for crs-libfuzzer and nasm
        for harness_result in self.harness_results:
            for pov_result in harness_result.pov_results:
                total_povs += 1
                if pov_result.status == POVStatus.FOUND:
                    povs_found += 1
                elif pov_result.status == POVStatus.MISSED:
                    povs_missed += 1
                elif pov_result.status == POVStatus.ERROR:
                    povs_error += 1

        return EvaluationReport(
            benchmark_path=self.benchmark_path,
            evaluation_mode=self.evaluation_mode,
            start_time=self.start_time,
            end_time=end_time,
            total_execution_time=total_execution_time,
            harness_results=self.harness_results,
            total_povs=total_povs,
            povs_found=povs_found,
            povs_missed=povs_missed,
            povs_error=povs_error,
            base_commit=self.base_commit,
            ref_commit=self.ref_commit,
            crs_config=self.crs_config
        )
