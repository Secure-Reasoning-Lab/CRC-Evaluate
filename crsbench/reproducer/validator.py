"""Core POV validation logic."""

import logging
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from pathlib import Path
from crsbench.validation.schemas import POV, HarnessFile
from crsbench.reproducer.harness import HarnessExecutor, ExecutionResult
from crsbench.reproducer.detector import SanitizerDetector, TimeoutDetector, CrashDetector

logger = logging.getLogger(__name__)


class ValidationStatus(Enum):
    """Status of POV validation."""
    VALID = "valid"                    # POV reproduces expected behavior
    INVALID = "invalid"                # POV does not reproduce expected behavior
    TIMEOUT = "timeout"                # Execution timed out unexpectedly
    BUILD_FAILED = "build_failed"      # Failed to build harness
    EXECUTION_ERROR = "execution_error" # Error during execution
    UNKNOWN = "unknown"                # Unable to determine validity


@dataclass
class ValidationResult:
    """Result of POV validation."""
    pov_name: str
    harness_name: str
    status: ValidationStatus
    expected_behavior: str
    actual_output: str
    execution_time: float
    sanitizer_triggered: bool = False
    crash_detected: bool = False
    timeout_occurred: bool = False
    error_message: Optional[str] = None
    confidence: float = 1.0


class POVValidator:
    """Validates POVs by executing them against harness files."""

    def __init__(self,
                 timeout_seconds: int = 30,
                 max_retries: int = 2,
                 build_timeout: int = 60):
        """Initialize POV validator.

        Args:
            timeout_seconds: Maximum execution time for harness
            max_retries: Number of retry attempts for failed executions
            build_timeout: Maximum time for building harness
        """
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.build_timeout = build_timeout

        # Initialize detectors
        self.sanitizer_detector = SanitizerDetector()
        self.timeout_detector = TimeoutDetector(timeout_seconds)
        self.crash_detector = CrashDetector()

        # Initialize harness executor
        self.executor = HarnessExecutor(
            timeout=timeout_seconds,
            build_timeout=build_timeout
        )

    def validate_pov(self,
                     pov: POV,
                     harness: HarnessFile,
                     benchmark_path: Path,
                     pov_input: Optional[bytes] = None) -> ValidationResult:
        """Validate a single POV against its harness.

        Args:
            pov: POV configuration from meta.yaml
            harness: Harness file configuration
            benchmark_path: Path to benchmark directory
            pov_input: Optional input data for the POV

        Returns:
            ValidationResult with validation outcome
        """
        logger.info(f"Validating POV '{pov.name}' against harness '{harness.name}'")

        # Resolve harness path
        harness_path = self._resolve_harness_path(harness.path, benchmark_path)
        if not harness_path.exists():
            return ValidationResult(
                pov_name=pov.name,
                harness_name=harness.name,
                status=ValidationStatus.BUILD_FAILED,
                expected_behavior=self._get_expected_behavior(pov),
                actual_output="",
                execution_time=0.0,
                error_message=f"Harness file not found: {harness_path}"
            )

        # Execute harness with POV input
        execution_result = self._execute_with_retries(
            harness_path,
            pov_input,
            pov.sanitizer
        )

        # Analyze execution result
        return self._analyze_execution(pov, harness, execution_result)

    def validate_multiple_povs(self,
                              povs: List[POV],
                              harness: HarnessFile,
                              benchmark_path: Path,
                              pov_inputs: Optional[Dict[str, bytes]] = None) -> List[ValidationResult]:
        """Validate multiple POVs against a single harness.

        Args:
            povs: List of POV configurations
            harness: Harness file configuration
            benchmark_path: Path to benchmark directory
            pov_inputs: Optional mapping of POV names to input data

        Returns:
            List of ValidationResult objects
        """
        results = []
        pov_inputs = pov_inputs or {}

        for pov in povs:
            pov_input = pov_inputs.get(pov.name)
            result = self.validate_pov(pov, harness, benchmark_path, pov_input)
            results.append(result)

        return results

    def _execute_with_retries(self,
                            harness_path: Path,
                            pov_input: Optional[bytes],
                            sanitizer: str) -> ExecutionResult:
        """Execute harness with retry logic."""
        last_result = None

        for attempt in range(self.max_retries + 1):
            try:
                logger.debug(f"Execution attempt {attempt + 1}/{self.max_retries + 1}")
                result = self.executor.execute_harness(
                    harness_path,
                    pov_input,
                    sanitizer
                )

                # If execution succeeded or failed deterministically, return
                if result.return_code != -1 or not result.stderr.strip():
                    return result

                last_result = result

            except Exception as e:
                logger.warning(f"Execution attempt {attempt + 1} failed: {e}")
                if attempt == self.max_retries:
                    # Create error result for final attempt
                    return ExecutionResult(
                        return_code=-1,
                        stdout="",
                        stderr=f"Execution failed after {self.max_retries + 1} attempts: {e}",
                        execution_time=0.0,
                        timed_out=False
                    )

        return last_result or ExecutionResult(
            return_code=-1,
            stdout="",
            stderr="All retry attempts failed",
            execution_time=0.0,
            timed_out=False
        )

    def _analyze_execution(self,
                          pov: POV,
                          harness: HarnessFile,
                          execution_result: ExecutionResult) -> ValidationResult:
        """Analyze execution result to determine POV validity."""
        expected_behavior = self._get_expected_behavior(pov)

        # Check for build/execution failures
        if execution_result.return_code == -1 and "compilation terminated" in execution_result.stderr:
            return ValidationResult(
                pov_name=pov.name,
                harness_name=harness.name,
                status=ValidationStatus.BUILD_FAILED,
                expected_behavior=expected_behavior,
                actual_output=execution_result.stderr,
                execution_time=execution_result.execution_time,
                error_message="Failed to build harness"
            )

        # Check for timeout
        if execution_result.timed_out:
            timeout_detected = self.timeout_detector.detect(execution_result)
            return ValidationResult(
                pov_name=pov.name,
                harness_name=harness.name,
                status=ValidationStatus.TIMEOUT,
                expected_behavior=expected_behavior,
                actual_output=execution_result.stderr,
                execution_time=execution_result.execution_time,
                timeout_occurred=timeout_detected
            )

        # Check for sanitizer triggers
        sanitizer_triggered = self.sanitizer_detector.detect(
            execution_result,
            pov.sanitizer,
            pov.error_token
        )

        # Check for crashes
        crash_detected = self.crash_detector.detect(execution_result)

        # Determine validation status
        status = self._determine_status(
            pov,
            sanitizer_triggered,
            crash_detected,
            execution_result
        )

        return ValidationResult(
            pov_name=pov.name,
            harness_name=harness.name,
            status=status,
            expected_behavior=expected_behavior,
            actual_output=execution_result.stderr or execution_result.stdout,
            execution_time=execution_result.execution_time,
            sanitizer_triggered=sanitizer_triggered,
            crash_detected=crash_detected,
            timeout_occurred=execution_result.timed_out,
            confidence=self._calculate_confidence(sanitizer_triggered, crash_detected, pov)
        )

    def _determine_status(self,
                         pov: POV,
                         sanitizer_triggered: bool,
                         crash_detected: bool,
                         execution_result: ExecutionResult) -> ValidationStatus:
        """Determine validation status based on detection results."""
        # If we expected sanitizer trigger and got it, POV is valid
        if pov.error_token and sanitizer_triggered:
            return ValidationStatus.VALID

        # If we expected a crash and got it, POV might be valid
        if crash_detected and execution_result.return_code != 0:
            return ValidationStatus.VALID

        # If sanitizer was expected but not triggered, POV is invalid
        if pov.error_token and not sanitizer_triggered:
            return ValidationStatus.INVALID

        # If execution completed normally but we expected an error, POV is invalid
        if execution_result.return_code == 0 and (pov.error_token or pov.sanitizer != "none"):
            return ValidationStatus.INVALID

        # Otherwise, unclear
        return ValidationStatus.UNKNOWN

    def _calculate_confidence(self,
                            sanitizer_triggered: bool,
                            crash_detected: bool,
                            pov: POV) -> float:
        """Calculate confidence score for validation result."""
        confidence = 0.5  # Base confidence

        # High confidence if sanitizer triggered as expected
        if pov.error_token and sanitizer_triggered:
            confidence = 0.95

        # Medium confidence if crash detected
        elif crash_detected:
            confidence = 0.75

        # Low confidence if no clear indicators
        elif not sanitizer_triggered and not crash_detected:
            confidence = 0.3

        return confidence

    def _resolve_harness_path(self, path: str, benchmark_path: Path) -> Path:
        """Resolve harness path variables."""
        resolved = path

        # Replace path variables
        if "$REPO/" in resolved:
            resolved = resolved.replace("$REPO/", str(benchmark_path) + "/")
        elif "$PROJECT/" in resolved:
            # For now, treat $PROJECT same as $REPO - this may need refinement
            resolved = resolved.replace("$PROJECT/", str(benchmark_path) + "/")

        return Path(resolved)

    def _get_expected_behavior(self, pov: POV) -> str:
        """Get expected behavior description for POV."""
        behaviors = []

        if pov.sanitizer and pov.sanitizer != "none":
            behaviors.append(f"{pov.sanitizer} sanitizer trigger")

        if pov.error_token:
            behaviors.append(f"error containing '{pov.error_token}'")

        return " and ".join(behaviors) if behaviors else "vulnerability detection"