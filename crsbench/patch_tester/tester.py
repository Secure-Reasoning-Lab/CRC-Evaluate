"""Core patch testing orchestrator."""

import logging
import time
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from crsbench.validation.schemas import POV, HarnessFile
from crsbench.reproducer import POVValidator, ValidationResult, ValidationStatus
from crsbench.patch_tester.applicator import PatchApplicator, PatchApplication, ApplicationStatus
from crsbench.patch_tester.git_manager import GitManager, GitOperation
from crsbench.patch_tester.validator import PatchValidator, ValidationOutcome

logger = logging.getLogger(__name__)


class PatchStatus(Enum):
    """Status of patch testing."""
    SUCCESS = "success"                    # Patch successfully fixes POV
    FAILURE = "failure"                    # Patch does not fix POV
    PARTIAL_SUCCESS = "partial_success"    # Patch fixes some but not all POVs
    APPLICATION_FAILED = "application_failed"  # Patch could not be applied
    BUILD_FAILED = "build_failed"          # Patched code fails to build
    VALIDATION_ERROR = "validation_error"   # Error during validation
    UNKNOWN = "unknown"                    # Unable to determine result


@dataclass
class PatchTestResult:
    """Result of testing a patch against POVs."""
    patch_name: str
    patch_content: str
    target_povs: List[str]
    status: PatchStatus

    # Before patch application
    pre_patch_results: List[ValidationResult]
    pre_patch_povs_triggered: int

    # After patch application
    post_patch_results: List[ValidationResult]
    post_patch_povs_triggered: int

    # Comparison
    povs_fixed: List[str]
    povs_still_triggered: List[str]
    povs_newly_broken: List[str]  # POVs that failed after patch but worked before

    # Metadata
    execution_time: float
    build_successful: bool
    application_successful: bool
    error_message: Optional[str] = None
    confidence: float = 1.0


class PatchTester:
    """Tests patches by applying them and validating POV behavior changes."""

    def __init__(self,
                 timeout_seconds: int = 60,
                 preserve_git_state: bool = True,
                 temp_branch_prefix: str = "crsbench-patch-test"):
        """Initialize patch tester.

        Args:
            timeout_seconds: Timeout for POV validation
            preserve_git_state: Whether to preserve original git state
            temp_branch_prefix: Prefix for temporary git branches
        """
        self.timeout_seconds = timeout_seconds
        self.preserve_git_state = preserve_git_state
        self.temp_branch_prefix = temp_branch_prefix

        # Initialize components
        self.pov_validator = POVValidator(timeout_seconds=timeout_seconds)
        self.patch_applicator = PatchApplicator()
        self.patch_validator = PatchValidator()
        self.git_manager = GitManager()

    def test_patch(self,
                   benchmark_path: Path,
                   patch_content: str,
                   patch_name: str,
                   target_povs: List[POV],
                   harness_files: List[HarnessFile],
                   pov_inputs: Optional[Dict[str, bytes]] = None) -> PatchTestResult:
        """Test a patch against target POVs.

        Args:
            benchmark_path: Path to benchmark directory
            patch_content: Patch content (unified diff format)
            patch_name: Name/identifier for the patch
            target_povs: List of POVs the patch should fix
            harness_files: List of harness files to test
            pov_inputs: Optional input data for POVs

        Returns:
            PatchTestResult with detailed test results
        """
        logger.info(f"Testing patch '{patch_name}' against {len(target_povs)} POVs")

        start_time = time.time()
        pov_inputs = pov_inputs or {}

        # Initialize result
        result = PatchTestResult(
            patch_name=patch_name,
            patch_content=patch_content,
            target_povs=[pov.name for pov in target_povs],
            status=PatchStatus.UNKNOWN,
            pre_patch_results=[],
            pre_patch_povs_triggered=0,
            post_patch_results=[],
            post_patch_povs_triggered=0,
            povs_fixed=[],
            povs_still_triggered=[],
            povs_newly_broken=[],
            execution_time=0.0,
            build_successful=False,
            application_successful=False
        )

        try:
            # Step 1: Test POVs before patch application
            logger.info("Testing POVs before patch application")
            result.pre_patch_results = self._test_povs(
                benchmark_path, target_povs, harness_files, pov_inputs
            )
            result.pre_patch_povs_triggered = sum(
                1 for r in result.pre_patch_results
                if r.status == ValidationStatus.VALID and r.sanitizer_triggered
            )

            logger.info(f"Pre-patch: {result.pre_patch_povs_triggered}/{len(target_povs)} POVs triggered")

            # Step 2: Apply patch
            if self.preserve_git_state:
                # Create temporary branch for testing
                branch_name = f"{self.temp_branch_prefix}-{int(time.time())}"
                logger.info(f"Creating temporary branch: {branch_name}")
                self.git_manager.create_branch(benchmark_path, branch_name)

            try:
                logger.info("Applying patch")
                patch_application = self.patch_applicator.apply_patch(
                    benchmark_path, patch_content, patch_name
                )

                result.application_successful = (patch_application.status == ApplicationStatus.SUCCESS)

                if not result.application_successful:
                    result.status = PatchStatus.APPLICATION_FAILED
                    result.error_message = patch_application.error_message
                    return result

                # Step 3: Validate patch
                logger.info("Validating patch application")
                validation_outcome = self.patch_validator.validate_patch(
                    benchmark_path, patch_application
                )

                if validation_outcome != ValidationOutcome.VALID:
                    result.status = PatchStatus.BUILD_FAILED
                    result.error_message = "Patch validation failed"
                    return result

                result.build_successful = True

                # Step 4: Test POVs after patch application
                logger.info("Testing POVs after patch application")
                result.post_patch_results = self._test_povs(
                    benchmark_path, target_povs, harness_files, pov_inputs
                )
                result.post_patch_povs_triggered = sum(
                    1 for r in result.post_patch_results
                    if r.status == ValidationStatus.VALID and r.sanitizer_triggered
                )

                logger.info(f"Post-patch: {result.post_patch_povs_triggered}/{len(target_povs)} POVs triggered")

                # Step 5: Analyze results
                result = self._analyze_patch_effectiveness(result)

            finally:
                # Restore git state if needed
                if self.preserve_git_state:
                    logger.info("Restoring original git state")
                    self.git_manager.restore_original_state(benchmark_path)

        except Exception as e:
            logger.error(f"Error testing patch '{patch_name}': {e}")
            result.status = PatchStatus.VALIDATION_ERROR
            result.error_message = str(e)

        finally:
            result.execution_time = time.time() - start_time

        logger.info(f"Patch test completed: {result.status.value}")
        return result

    def test_multiple_patches(self,
                            benchmark_path: Path,
                            patches: List[Tuple[str, str, List[POV]]],  # (name, content, target_povs)
                            harness_files: List[HarnessFile],
                            pov_inputs: Optional[Dict[str, bytes]] = None) -> List[PatchTestResult]:
        """Test multiple patches sequentially.

        Args:
            benchmark_path: Path to benchmark directory
            patches: List of (patch_name, patch_content, target_povs) tuples
            harness_files: List of harness files to test
            pov_inputs: Optional input data for POVs

        Returns:
            List of PatchTestResult objects
        """
        results = []

        for patch_name, patch_content, target_povs in patches:
            logger.info(f"Testing patch {len(results) + 1}/{len(patches)}: {patch_name}")

            result = self.test_patch(
                benchmark_path, patch_content, patch_name,
                target_povs, harness_files, pov_inputs
            )
            results.append(result)

        return results

    def _test_povs(self,
                   benchmark_path: Path,
                   povs: List[POV],
                   harness_files: List[HarnessFile],
                   pov_inputs: Dict[str, bytes]) -> List[ValidationResult]:
        """Test POVs using reproducer module."""
        results = []

        # Group POVs by harness
        harness_povs = {}
        for pov in povs:
            for harness in harness_files:
                if pov in harness.povs:
                    if harness.name not in harness_povs:
                        harness_povs[harness.name] = []
                    harness_povs[harness.name].append((pov, harness))
                    break

        # Test each harness
        for harness_name, pov_harness_pairs in harness_povs.items():
            logger.debug(f"Testing {len(pov_harness_pairs)} POVs for harness {harness_name}")

            for pov, harness in pov_harness_pairs:
                pov_input = pov_inputs.get(pov.name)

                validation_result = self.pov_validator.validate_pov(
                    pov, harness, benchmark_path, pov_input
                )
                results.append(validation_result)

        return results

    def _analyze_patch_effectiveness(self, result: PatchTestResult) -> PatchTestResult:
        """Analyze patch effectiveness by comparing pre/post results."""

        # Create maps for easier comparison
        pre_results = {r.pov_name: r for r in result.pre_patch_results}
        post_results = {r.pov_name: r for r in result.post_patch_results}

        for pov_name in result.target_povs:
            pre_result = pre_results.get(pov_name)
            post_result = post_results.get(pov_name)

            if not pre_result or not post_result:
                continue

            pre_triggered = (pre_result.status == ValidationStatus.VALID and
                           pre_result.sanitizer_triggered)
            post_triggered = (post_result.status == ValidationStatus.VALID and
                            post_result.sanitizer_triggered)

            if pre_triggered and not post_triggered:
                # POV was fixed
                result.povs_fixed.append(pov_name)
            elif pre_triggered and post_triggered:
                # POV still triggers
                result.povs_still_triggered.append(pov_name)
            elif not pre_triggered and post_triggered:
                # POV newly broken (unexpected)
                result.povs_newly_broken.append(pov_name)

        # Determine overall patch status
        if len(result.povs_fixed) == len(result.target_povs) and len(result.povs_newly_broken) == 0:
            result.status = PatchStatus.SUCCESS
            result.confidence = 0.95
        elif len(result.povs_fixed) > 0 and len(result.povs_still_triggered) > 0:
            result.status = PatchStatus.PARTIAL_SUCCESS
            result.confidence = 0.7
        elif len(result.povs_fixed) == 0:
            result.status = PatchStatus.FAILURE
            result.confidence = 0.9
        else:
            result.status = PatchStatus.UNKNOWN
            result.confidence = 0.5

        # Reduce confidence if newly broken POVs
        if len(result.povs_newly_broken) > 0:
            result.confidence *= 0.8

        logger.info(f"Patch analysis: {len(result.povs_fixed)} fixed, "
                   f"{len(result.povs_still_triggered)} still triggered, "
                   f"{len(result.povs_newly_broken)} newly broken")

        return result