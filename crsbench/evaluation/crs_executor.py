"""CRS executor interface and stub implementation."""

import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any
from crsbench.validation.schemas import BenchmarkConfig, HarnessFile, POV
from crsbench.evaluation.results import POVResult, HarnessResult, POVStatus


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
    def run_crs(self, benchmark_path: Path, harness: HarnessFile,
                base_commit: str, ref_commit: Optional[str] = None) -> CRSResult:
        """Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness file configuration
            base_commit: Base commit hash
            ref_commit: Reference commit hash (for delta mode)

        Returns:
            CRSResult: Result of CRS execution
        """
        pass

    @abstractmethod
    def process_pov_results(self, crs_result: CRSResult, harness: HarnessFile) -> List[POVResult]:
        """Process CRS results to determine POV detection status.

        Args:
            crs_result: Raw CRS execution result
            harness: Harness configuration with POVs

        Returns:
            List[POVResult]: POV detection results
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

    def run_crs(self, benchmark_path: Path, harness: HarnessFile,
                base_commit: str, ref_commit: Optional[str] = None) -> CRSResult:
        """Run stub CRS execution."""
        start_time = time.time()

        # Simulate execution time
        time.sleep(self.simulation_delay)

        execution_time = time.time() - start_time

        # Simulate random success/failure
        success = random.random() < 0.95  # 95% execution success rate

        if success:
            # Generate mock CRS output
            mode = "delta" if ref_commit else "full"
            output = f"""
CRS Execution Results for {harness.name}
Mode: {mode}
Base commit: {base_commit}
{f"Ref commit: {ref_commit}" if ref_commit else ""}
Benchmark path: {benchmark_path}
Harness path: {harness.path}

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
                        detected_povs.append(pov.name)
                        output += f"✓ POV '{pov.name}' detected - {pov.sanitizer} sanitizer triggered\n"
                        output += f"  Error pattern: {pov.error_token}\n"
                    else:
                        output += f"✗ POV '{pov.name}' not detected\n"

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

    def process_pov_results(self, crs_result: CRSResult, harness: HarnessFile) -> List[POVResult]:
        """Process stub CRS results to determine POV status."""
        pov_results = []

        if not harness.povs:
            return pov_results

        if not crs_result.success:
            # If CRS execution failed, mark all POVs as error
            for pov in harness.povs:
                pov_results.append(POVResult(
                    name=pov.name,
                    harness_name=harness.name,
                    sanitizer=pov.sanitizer,
                    error_token=pov.error_token,
                    status=POVStatus.ERROR,
                    error_message=crs_result.error,
                    crs_output=crs_result.output
                ))
            return pov_results

        # Process each POV
        detected_povs = crs_result.povs_detected or []

        for pov in harness.povs:
            if pov.name in detected_povs:
                status = POVStatus.FOUND
            else:
                status = POVStatus.MISSED

            pov_results.append(POVResult(
                name=pov.name,
                harness_name=harness.name,
                sanitizer=pov.sanitizer,
                error_token=pov.error_token,
                status=status,
                execution_time=crs_result.execution_time / len(harness.povs),
                crs_output=crs_result.output
            ))

        return pov_results