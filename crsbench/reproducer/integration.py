"""Integration layer for reproducer with evaluation results."""

import logging
from typing import List, Dict, Optional
from pathlib import Path
from crsbench.evaluation.results import EvaluationReport, HarnessResult, POVResult, POVStatus
from crsbench.reproducer.validator import POVValidator, ValidationResult, ValidationStatus

logger = logging.getLogger(__name__)


def validate_evaluation_povs(evaluation_report: EvaluationReport,
                           benchmark_path: Path,
                           timeout_seconds: int = 30,
                           pov_inputs: Optional[Dict[str, bytes]] = None) -> Dict[str, List[ValidationResult]]:
    """Validate POVs from evaluation results by reproducing them.

    Args:
        evaluation_report: Results from CRS evaluation
        benchmark_path: Path to benchmark directory
        timeout_seconds: Timeout for POV execution
        pov_inputs: Optional mapping of POV names to input data

    Returns:
        Dictionary mapping harness names to validation results
    """
    logger.info(f"Validating POVs from evaluation report for benchmark: {benchmark_path}")

    validator = POVValidator(timeout_seconds=timeout_seconds)
    pov_inputs = pov_inputs or {}

    validation_results = {}

    for harness_result in evaluation_report.harness_results:
        harness_name = harness_result.name
        logger.info(f"Validating harness: {harness_name}")

        harness_validations = []

        # Validate each POV that was reported as found
        for pov_result in harness_result.pov_results:
            if pov_result.status == POVStatus.FOUND:
                logger.debug(f"Validating POV: {pov_result.name}")

                # Get POV input if available
                pov_input = pov_inputs.get(pov_result.name)

                # Create POV and harness objects for validation
                # Note: In a real implementation, we would need to reconstruct
                # these from the benchmark configuration
                validation_result = _validate_single_pov(
                    validator,
                    pov_result,
                    harness_result,
                    benchmark_path,
                    pov_input
                )

                harness_validations.append(validation_result)

        validation_results[harness_name] = harness_validations

    return validation_results


def create_validation_summary(validation_results: Dict[str, List[ValidationResult]]) -> Dict[str, any]:
    """Create a summary of validation results.

    Args:
        validation_results: Validation results by harness

    Returns:
        Summary statistics and details
    """
    total_povs = 0
    valid_povs = 0
    invalid_povs = 0
    timeout_povs = 0
    error_povs = 0

    by_status = {status: 0 for status in ValidationStatus}
    details = {}

    for harness_name, results in validation_results.items():
        harness_summary = {
            'total': len(results),
            'valid': 0,
            'invalid': 0,
            'timeout': 0,
            'errors': 0,
            'povs': []
        }

        for result in results:
            total_povs += 1
            by_status[result.status] += 1

            if result.status == ValidationStatus.VALID:
                valid_povs += 1
                harness_summary['valid'] += 1
            elif result.status == ValidationStatus.INVALID:
                invalid_povs += 1
                harness_summary['invalid'] += 1
            elif result.status == ValidationStatus.TIMEOUT:
                timeout_povs += 1
                harness_summary['timeout'] += 1
            else:
                error_povs += 1
                harness_summary['errors'] += 1

            harness_summary['povs'].append({
                'name': result.pov_name,
                'status': result.status.value,
                'confidence': result.confidence,
                'sanitizer_triggered': result.sanitizer_triggered,
                'crash_detected': result.crash_detected,
                'timeout_occurred': result.timeout_occurred,
                'execution_time': result.execution_time
            })

        details[harness_name] = harness_summary

    return {
        'summary': {
            'total_povs': total_povs,
            'valid_povs': valid_povs,
            'invalid_povs': invalid_povs,
            'timeout_povs': timeout_povs,
            'error_povs': error_povs,
            'validation_rate': valid_povs / total_povs if total_povs > 0 else 0.0,
            'by_status': {status.value: count for status, count in by_status.items()}
        },
        'by_harness': details
    }


def filter_valid_povs(validation_results: Dict[str, List[ValidationResult]],
                     min_confidence: float = 0.7) -> Dict[str, List[ValidationResult]]:
    """Filter validation results to only include valid POVs with sufficient confidence.

    Args:
        validation_results: Validation results by harness
        min_confidence: Minimum confidence threshold

    Returns:
        Filtered validation results
    """
    filtered_results = {}

    for harness_name, results in validation_results.items():
        valid_results = [
            result for result in results
            if result.status == ValidationStatus.VALID and result.confidence >= min_confidence
        ]
        filtered_results[harness_name] = valid_results

    return filtered_results


def export_validation_results(validation_results: Dict[str, List[ValidationResult]],
                            output_path: Path,
                            format: str = "json") -> None:
    """Export validation results to file.

    Args:
        validation_results: Validation results to export
        output_path: Output file path
        format: Export format (json or yaml)
    """
    import json
    import yaml

    # Convert results to serializable format
    export_data = {}
    for harness_name, results in validation_results.items():
        export_data[harness_name] = []
        for result in results:
            export_data[harness_name].append({
                'pov_name': result.pov_name,
                'harness_name': result.harness_name,
                'status': result.status.value,
                'expected_behavior': result.expected_behavior,
                'actual_output': result.actual_output,
                'execution_time': result.execution_time,
                'sanitizer_triggered': result.sanitizer_triggered,
                'crash_detected': result.crash_detected,
                'timeout_occurred': result.timeout_occurred,
                'error_message': result.error_message,
                'confidence': result.confidence
            })

    # Write to file
    with open(output_path, 'w') as f:
        if format.lower() == 'json':
            json.dump(export_data, f, indent=2)
        elif format.lower() == 'yaml':
            yaml.dump(export_data, f, default_flow_style=False)
        else:
            raise ValueError(f"Unsupported format: {format}")

    logger.info(f"Validation results exported to: {output_path}")


def _validate_single_pov(validator: POVValidator,
                        pov_result: POVResult,
                        harness_result: HarnessResult,
                        benchmark_path: Path,
                        pov_input: Optional[bytes]) -> ValidationResult:
    """Validate a single POV result.

    Args:
        validator: POV validator instance
        pov_result: POV result from evaluation
        harness_result: Harness result from evaluation
        benchmark_path: Path to benchmark directory
        pov_input: Optional input data for POV

    Returns:
        ValidationResult for the POV
    """
    # This is a stub implementation
    # In practice, we would need to reconstruct the POV and harness configurations
    # from the benchmark meta.yaml file and the evaluation results

    try:
        # For now, create a basic validation result
        # In a full implementation, we would:
        # 1. Load the benchmark configuration
        # 2. Find the POV and harness definitions
        # 3. Run the actual validation

        logger.warning("POV validation not fully implemented - returning stub result")

        return ValidationResult(
            pov_name=pov_result.name,
            harness_name=harness_result.name,
            status=ValidationStatus.UNKNOWN,
            expected_behavior="Vulnerability detection",
            actual_output="Validation not implemented",
            execution_time=0.0,
            error_message="POV validation implementation incomplete"
        )

    except Exception as e:
        logger.error(f"Error validating POV {pov_result.name}: {e}")
        return ValidationResult(
            pov_name=pov_result.name,
            harness_name=harness_result.name,
            status=ValidationStatus.EXECUTION_ERROR,
            expected_behavior="Vulnerability detection",
            actual_output="",
            execution_time=0.0,
            error_message=str(e)
        )


def validate_pov_with_benchmark_config(benchmark_path: Path,
                                     pov_name: str,
                                     harness_name: str,
                                     pov_input: Optional[bytes] = None,
                                     timeout_seconds: int = 30) -> ValidationResult:
    """Validate a POV using benchmark configuration.

    Args:
        benchmark_path: Path to benchmark directory
        pov_name: Name of POV to validate
        harness_name: Name of harness to use
        pov_input: Optional input data for POV
        timeout_seconds: Timeout for execution

    Returns:
        ValidationResult for the POV
    """
    from crsbench.validation import validate_benchmark

    # Load and validate benchmark configuration
    validation_result = validate_benchmark(benchmark_path)
    if not validation_result.is_valid:
        return ValidationResult(
            pov_name=pov_name,
            harness_name=harness_name,
            status=ValidationStatus.EXECUTION_ERROR,
            expected_behavior="Valid benchmark configuration",
            actual_output="",
            execution_time=0.0,
            error_message="Invalid benchmark configuration"
        )

    config = validation_result.config

    # Find harness and POV
    harness = None
    pov = None

    for harness_file in config.harness_files:
        if harness_file.name == harness_name:
            harness = harness_file
            for pov_config in harness_file.povs:
                if pov_config.name == pov_name:
                    pov = pov_config
                    break
            break

    if not harness or not pov:
        return ValidationResult(
            pov_name=pov_name,
            harness_name=harness_name,
            status=ValidationStatus.EXECUTION_ERROR,
            expected_behavior="Valid harness and POV configuration",
            actual_output="",
            execution_time=0.0,
            error_message=f"Harness '{harness_name}' or POV '{pov_name}' not found"
        )

    # Create validator and run validation
    validator = POVValidator(timeout_seconds=timeout_seconds)
    return validator.validate_pov(pov, harness, benchmark_path, pov_input)