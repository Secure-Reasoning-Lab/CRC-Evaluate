"""Utility classes and functions for benchmark CI testing."""

import enum
from dataclasses import dataclass, field
from typing import Optional, List, Set


class TaskMode(enum.Enum):
    """Execution mode for benchmark tasks."""
    DELTA = "delta"
    FULL = "full"


class ExecJobType(enum.Enum):
    """Type of execution job for benchmark testing."""
    DELTA_BASE_CHECK = "delta_base_check"  # Build and test at base commit (clean, delta mode)
    DELTA_REF_CHECK = "delta_ref_check"    # Build and test at ref commit (vulnerable, delta mode)
    FULL_BASE_CHECK = "full_base_check"    # Build and test at base commit (vulnerable, full mode)
    POV_CHECK = "pov_check"                # Verify POV reproduction
    PATCH_CHECK = "patch_check"            # Verify patch fixes the vulnerability
    TEST_SH_CHECK = "test_sh_check"        # Verify test.sh execution
    TEST_INC_BUILD = "test_inc_build"      # Test incremental build (oss-bugfix-crs test-inc-build)


@dataclass
class Task:
    """Represents a benchmark task (delta or full mode).

    Delta mode: base_commit (clean) → ref_commit (vulnerable)
    Full mode: base_commit (vulnerable), no ref_commit
    """
    mode: TaskMode
    base_commit: str
    ref_commit: Optional[str] = None  # Only for delta mode (vulnerable version)

    def __str__(self):
        if self.mode == TaskMode.DELTA:
            return f"Task(mode={self.mode.value}, base={self.base_commit[:8]}, ref={self.ref_commit[:8]})"
        return f"Task(mode={self.mode.value}, base={self.base_commit[:8]})"


@dataclass
class POV:
    """Represents a Proof of Vulnerability."""
    id: str
    sanitizer: str
    error_token: str
    blob_path: str  # Path to POV blob file

    def __str__(self):
        return f"POV(id={self.id}, sanitizer={self.sanitizer})"


@dataclass
class Vulnerability:
    """Represents a vulnerability (CPV in OSS-Fuzz terms)."""
    id: str  # e.g., "cpv_0"
    name: str
    cwes: List[str]
    povs: List[POV] = field(default_factory=list)
    patch_path: Optional[str] = None  # Path to patch file

    def __str__(self):
        return f"Vuln(id={self.id}, name={self.name}, povs={len(self.povs)})"


@dataclass
class Harness:
    """Represents a fuzzing harness."""
    name: str
    path: str  # Path from meta.yaml
    vulnerabilities: List[Vulnerability] = field(default_factory=list)

    def __str__(self):
        return f"Harness(name={self.name}, vulns={len(self.vulnerabilities)})"


@dataclass
class JobContext:
    """Context for a single test job."""
    job_type: ExecJobType
    task: Task
    benchmark: str
    language: str
    engine: str
    sanitizer: str
    harness: Optional[Harness] = None
    vulnerability: Optional[Vulnerability] = None
    pov: Optional[POV] = None
    vulns_in_context: Optional[Set[str]] = None  # For delta mode filtering

    def __str__(self):
        base = f"Job({self.job_type.value}, {self.benchmark}, {self.engine}/{self.sanitizer}"
        if self.harness:
            base += f", harness={self.harness.name}"
        if self.vulnerability:
            base += f", vuln={self.vulnerability.id}"
        if self.pov:
            base += f", pov={self.pov.id}"
        base += ")"
        return base

    def __lt__(self, other):
        """Sorting for consistent job ordering."""
        return (
            self.benchmark,
            self.job_type.value,
            self.engine,
            self.sanitizer,
            self.harness.name if self.harness else "",
            self.vulnerability.id if self.vulnerability else "",
            self.pov.id if self.pov else "",
        ) < (
            other.benchmark,
            other.job_type.value,
            other.engine,
            other.sanitizer,
            other.harness.name if other.harness else "",
            other.vulnerability.id if other.vulnerability else "",
            other.pov.id if other.pov else "",
        )


def get_benchmarks_root() -> str:
    """Get the path to benchmarks directory."""
    import os
    from pathlib import Path

    crsbench_root = Path(__file__).parent.parent.parent
    benchmarks_path = crsbench_root / "benchmarks"

    if not benchmarks_path.exists():
        raise RuntimeError(f"Benchmarks directory not found at {benchmarks_path}")

    return str(benchmarks_path)
