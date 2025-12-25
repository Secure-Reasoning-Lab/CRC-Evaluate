"""CRS executor interface and stub implementation.

NOTE: When implementing concrete CRS executors (CRSBugFindingExecutor, CRSPatchExecutor),
use the oss-crs CLI with trial-specific parameters:

Bug Finding (oss-bugfind-crs):
    Build:  ["oss-bugfind-crs", "build",
             "--build-dir", trial_build_dir,
             "--oss-fuzz-dir", oss_fuzz_submodule,
             "--registry-dir", crs_registry_dir,
             "--project-path", benchmark_dir,
             config_name, project_name, source_path]

    Run:    ["oss-bugfind-crs", "run",
             "--build-dir", trial_build_dir,
             "--oss-fuzz-dir", oss_fuzz_submodule,
             "--registry-dir", crs_registry_dir,
             config_name, project_name, harness_name,
             "--output", output_dir, "--hints", hints_dir]

Patch Generation (oss-bugfix-crs):
    Build:  ["oss-bugfix-crs", "build",
             "--build-dir", trial_build_dir,
             "--oss-fuzz-dir", oss_fuzz_submodule,
             "--registry-dir", crs_registry_dir,
             "--project-path", benchmark_dir,
             config_name, project_name, source_path]

    Run:    ["oss-bugfix-crs", "run",
             "--build-dir", trial_build_dir,
             "--oss-fuzz-dir", oss_fuzz_submodule,
             "--registry-dir", crs_registry_dir,
             config_name, project_name,
             "--harness", harness_name,
             "--povs", povs_dir, "--hints", hints_dir,
             "--output", output_dir,
             "--litellm-base", url, "--litellm-key", key]

Trial directory preparation handled by TrialDirectoryPreparer.
See design-docs/evaluation/oss-crs-integration.md for CLI parameters.
See design-docs/evaluation/trial-directory-preparation.md for directory structure.
See design-docs/evaluation/crs-executors.md for executor implementation.
"""

import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any
from crsbench.validation.schemas import BenchmarkConfig, HarnessFile, POV
from crsbench.evaluation.results import HarnessResult


@dataclass
class CRSResult:
    """Result from CRS execution."""
    harness_name: str
    execution_time: float
    success: bool
    output: str
    error: Optional[str] = None
    povs_detected: Optional[List[str]] = None


class CRSExecutor(ABC):
    """Abstract base class for CRS executors."""

    @abstractmethod
    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS with given parameters.

        Args:
            config: CRS configuration parameters
        """
        pass

    @abstractmethod
    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> CRSResult:
        """Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness file configuration
            trial_output_dir: Directory for this trial's outputs

        Returns:
            CRSResult: Result of CRS execution

        Note:
            Source code is already prepared at the correct commit by
            TrialDirectoryPreparer. The executor does not need commit
            information - it simply runs CRS on pre-prepared directories.
        """
        pass


class StubCRSExecutor(CRSExecutor):
    """Stub implementation of CRS executor for testing and development."""

    def __init__(self):
        self.config: Dict[str, Any] = {}
        self.simulation_delay = 0.5  # Simulate execution time
        self.success_rate = 0.7  # Simulate 70% POV detection rate

    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the stub CRS."""
        self.config = config.copy()

        # Extract configuration parameters if provided
        if 'simulation_delay' in config:
            self.simulation_delay = config['simulation_delay']
        if 'success_rate' in config:
            self.success_rate = config['success_rate']

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> CRSResult:
        """Run stub CRS execution."""
        start_time = time.time()

        # Simulate execution time
        time.sleep(self.simulation_delay)

        execution_time = time.time() - start_time

        # Simulate random success/failure
        success = random.random() < 0.95  # 95% execution success rate

        if success:
            # Generate mock CRS output
            output = f"""
CRS Execution Results for {harness.name}
Benchmark path: {benchmark_path}
Harness path: {harness.path}
Trial directory: {trial_output_dir}

Analyzing harness for potential vulnerabilities...
Running fuzzing campaign...
Collecting sanitizer outputs...

POVs analyzed: {len(harness.povs or [])}
"""

            # Simulate POV detection
            detected_povs = []
            if harness.povs:
                for pov in harness.povs:
                    if random.random() < self.success_rate:
                        detected_povs.append(pov.id)
                        output += f"✓ POV '{pov.id}' detected - {pov.sanitizer} sanitizer triggered\n"
                        output += f"  Error pattern: {pov.error_token}\n"
                    else:
                        output += f"✗ POV '{pov.id}' not detected\n"

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=True,
                output=output.strip(),
                povs_detected=detected_povs
            )
        else:
            # Simulate execution failure
            error_messages = [
                "Build failed: compilation error",
                "Runtime error: harness crashed",
                "Timeout: execution exceeded time limit",
                "Configuration error: invalid parameters"
            ]
            error = random.choice(error_messages)

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output=f"CRS execution failed for {harness.name}",
                error=error
            )