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

import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from crsbench.validation.schemas import HarnessFile


@dataclass
class CRSExecutionResult:
    """Result from CRS execution."""

    harness_name: str
    execution_time: float
    success: bool
    output: str
    error: Optional[str] = None
    timed_out: bool = False


class CRSExecutor(ABC):
    """Abstract base class for CRS executors."""

    @abstractmethod
    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS with given parameters.

        Args:
            config: CRS configuration parameters
        """

    @abstractmethod
    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path,
        *,
        on_run_start: Optional[Callable[[], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> CRSExecutionResult:
        """Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness file configuration
            trial_output_dir: Directory for this trial's outputs
            on_run_start: Callback invoked when CRS run starts (after build)
            stop_event: Optional event to signal early termination

        Returns:
            CRSExecutionResult: Result of CRS execution

        Note:
            Source code is already prepared at the correct commit by
            TrialDirectoryPreparer. The executor does not need commit
            information - it simply runs CRS on pre-prepared directories.
        """


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
        if "simulation_delay" in config:
            self.simulation_delay = config["simulation_delay"]
        if "success_rate" in config:
            self.success_rate = config["success_rate"]

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path,
        *,
        on_run_start: Optional[Callable[[], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> CRSExecutionResult:
        """Run stub CRS execution."""
        start_time = time.time()

        # Signal that run is starting (stub has no build phase)
        if on_run_start:
            on_run_start()

        # Simulate execution time (check for early stop)
        if stop_event:
            # Poll-based wait to support early stop
            elapsed = 0.0
            while elapsed < self.simulation_delay:
                if stop_event.is_set():
                    break
                time.sleep(min(0.1, self.simulation_delay - elapsed))
                elapsed = time.time() - start_time
        else:
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

            return CRSExecutionResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=True,
                output=output.strip(),
            )
        # Simulate execution failure
        error_messages = [
            "Build failed: compilation error",
            "Runtime error: harness crashed",
            "Timeout: execution exceeded time limit",
            "Configuration error: invalid parameters",
        ]
        error = random.choice(error_messages)

        return CRSExecutionResult(
            harness_name=harness.name,
            execution_time=execution_time,
            success=False,
            output=f"CRS execution failed for {harness.name}",
            error=error,
        )
