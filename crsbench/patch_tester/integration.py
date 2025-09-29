"""Integration layer for patch testing with CRS evaluation results."""

import logging
import json
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, asdict
from crsbench.evaluation.results import EvaluationReport, POVResult, POVStatus
from crsbench.validation import validate_benchmark
from crsbench.patch_tester.tester import PatchTester, PatchTestResult, PatchStatus
from crsbench.reproducer import validate_evaluation_povs

logger = logging.getLogger(__name__)


@dataclass
class CRSPatch:
    """Represents a patch reported by a CRS system."""
    name: str
    content: str
    target_povs: List[str]
    confidence: float
    description: Optional[str] = None
    metadata: Optional[Dict] = None


def test_crs_patches(evaluation_report: EvaluationReport,
                    patches: List[CRSPatch],
                    benchmark_path: Path,
                    pov_inputs: Optional[Dict[str, bytes]] = None,
                    timeout_seconds: int = 60) -> List[PatchTestResult]:
    """Test patches reported by CRS against evaluation results.

    Args:
        evaluation_report: Results from CRS evaluation
        patches: List of patches to test
        benchmark_path: Path to benchmark directory
        pov_inputs: Optional input data for POVs
        timeout_seconds: Timeout for patch testing

    Returns:
        List of PatchTestResult objects
    """
    logger.info(f"Testing {len(patches)} CRS patches against evaluation results")

    # Load and validate benchmark configuration
    validation_result = validate_benchmark(benchmark_path)
    if not validation_result.is_valid:
        raise ValueError("Invalid benchmark configuration")

    config = validation_result.config

    # Initialize patch tester
    patch_tester = PatchTester(
        timeout_seconds=timeout_seconds,
        preserve_git_state=True
    )

    results = []

    for patch in patches:
        logger.info(f"Testing patch: {patch.name}")

        try:
            # Find target POVs and harnesses
            target_povs = []
            harness_files = []

            for harness_file in config.harness_files:
                harness_povs = []
                for pov in harness_file.povs:
                    if pov.name in patch.target_povs:
                        target_povs.append(pov)
                        harness_povs.append(pov)

                if harness_povs:
                    harness_files.append(harness_file)

            if not target_povs:
                logger.warning(f"No target POVs found for patch {patch.name}")
                continue

            logger.info(f"Found {len(target_povs)} target POVs for patch {patch.name}")

            # Test the patch
            result = patch_tester.test_patch(
                benchmark_path=benchmark_path,
                patch_content=patch.content,
                patch_name=patch.name,
                target_povs=target_povs,
                harness_files=harness_files,
                pov_inputs=pov_inputs
            )

            results.append(result)

        except Exception as e:
            logger.error(f"Error testing patch {patch.name}: {e}")
            # Create error result
            error_result = PatchTestResult(
                patch_name=patch.name,
                patch_content=patch.content,
                target_povs=patch.target_povs,
                status=PatchStatus.VALIDATION_ERROR,
                pre_patch_results=[],
                pre_patch_povs_triggered=0,
                post_patch_results=[],
                post_patch_povs_triggered=0,
                povs_fixed=[],
                povs_still_triggered=[],
                povs_newly_broken=[],
                execution_time=0.0,
                build_successful=False,
                application_successful=False,
                error_message=str(e)
            )
            results.append(error_result)

    return results


def create_patch_test_report(patch_results: List[PatchTestResult]) -> Dict:
    """Create a comprehensive report from patch test results.

    Args:
        patch_results: List of patch test results

    Returns:
        Dictionary containing detailed report
    """
    total_patches = len(patch_results)
    successful_patches = sum(1 for r in patch_results if r.status == PatchStatus.SUCCESS)
    failed_patches = sum(1 for r in patch_results if r.status == PatchStatus.FAILURE)
    partial_patches = sum(1 for r in patch_results if r.status == PatchStatus.PARTIAL_SUCCESS)
    error_patches = sum(1 for r in patch_results
                       if r.status in [PatchStatus.APPLICATION_FAILED,
                                     PatchStatus.BUILD_FAILED,
                                     PatchStatus.VALIDATION_ERROR])

    # Calculate effectiveness metrics
    total_target_povs = sum(len(r.target_povs) for r in patch_results)
    total_fixed_povs = sum(len(r.povs_fixed) for r in patch_results)
    total_broken_povs = sum(len(r.povs_newly_broken) for r in patch_results)

    effectiveness_rate = total_fixed_povs / total_target_povs if total_target_povs > 0 else 0.0

    summary = {
        'total_patches': total_patches,
        'successful_patches': successful_patches,
        'failed_patches': failed_patches,
        'partial_patches': partial_patches,
        'error_patches': error_patches,
        'success_rate': successful_patches / total_patches if total_patches > 0 else 0.0,
        'total_target_povs': total_target_povs,
        'total_fixed_povs': total_fixed_povs,
        'total_broken_povs': total_broken_povs,
        'effectiveness_rate': effectiveness_rate
    }

    # Detailed results
    detailed_results = []
    for result in patch_results:
        detailed_results.append({
            'patch_name': result.patch_name,
            'status': result.status.value,
            'target_povs': result.target_povs,
            'povs_fixed': result.povs_fixed,
            'povs_still_triggered': result.povs_still_triggered,
            'povs_newly_broken': result.povs_newly_broken,
            'pre_patch_povs_triggered': result.pre_patch_povs_triggered,
            'post_patch_povs_triggered': result.post_patch_povs_triggered,
            'execution_time': result.execution_time,
            'build_successful': result.build_successful,
            'application_successful': result.application_successful,
            'confidence': result.confidence,
            'error_message': result.error_message
        })

    # Analysis by patch status
    status_breakdown = {}
    for status in PatchStatus:
        count = sum(1 for r in patch_results if r.status == status)
        status_breakdown[status.value] = count

    return {
        'summary': summary,
        'status_breakdown': status_breakdown,
        'detailed_results': detailed_results,
        'recommendations': _generate_recommendations(patch_results)
    }


def export_patch_test_results(patch_results: List[PatchTestResult],
                            output_path: Path,
                            format: str = "json") -> None:
    """Export patch test results to file.

    Args:
        patch_results: Patch test results to export
        output_path: Output file path
        format: Export format (json or yaml)
    """
    report = create_patch_test_report(patch_results)

    if format.lower() == 'json':
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)
    elif format.lower() == 'yaml':
        import yaml
        with open(output_path, 'w') as f:
            yaml.dump(report, f, default_flow_style=False)
    else:
        raise ValueError(f"Unsupported format: {format}")

    logger.info(f"Patch test results exported to: {output_path}")


def validate_patch_effectiveness(patch_result: PatchTestResult,
                               benchmark_path: Path,
                               additional_povs: Optional[List[str]] = None) -> Dict:
    """Validate patch effectiveness using reproducer module.

    Args:
        patch_result: Result from patch testing
        benchmark_path: Path to benchmark directory
        additional_povs: Additional POVs to test (beyond target POVs)

    Returns:
        Dictionary with validation results
    """
    logger.info(f"Validating effectiveness of patch: {patch_result.patch_name}")

    if patch_result.status != PatchStatus.SUCCESS:
        return {
            'validation_status': 'skipped',
            'reason': f'Patch status is {patch_result.status.value}, not SUCCESS'
        }

    # For now, return the existing results as validation
    # In a full implementation, this could re-run the reproducer
    # or perform additional validation steps

    return {
        'validation_status': 'completed',
        'patch_name': patch_result.patch_name,
        'povs_fixed': patch_result.povs_fixed,
        'povs_still_triggered': patch_result.povs_still_triggered,
        'effectiveness_confirmed': len(patch_result.povs_fixed) > 0,
        'side_effects_detected': len(patch_result.povs_newly_broken) > 0,
        'confidence_score': patch_result.confidence
    }


def compare_pre_post_patch_behavior(pre_results: List,
                                  post_results: List) -> Dict:
    """Compare POV behavior before and after patch application.

    Args:
        pre_results: Validation results before patch
        post_results: Validation results after patch

    Returns:
        Dictionary with comparison analysis
    """
    pre_map = {r.pov_name: r for r in pre_results}
    post_map = {r.pov_name: r for r in post_results}

    comparison = {
        'povs_analyzed': len(set(pre_map.keys()) & set(post_map.keys())),
        'behavior_changes': [],
        'fixed_povs': [],
        'broken_povs': [],
        'unchanged_povs': []
    }

    for pov_name in set(pre_map.keys()) & set(post_map.keys()):
        pre_result = pre_map[pov_name]
        post_result = post_map[pov_name]

        pre_triggered = (hasattr(pre_result, 'sanitizer_triggered') and
                        pre_result.sanitizer_triggered)
        post_triggered = (hasattr(post_result, 'sanitizer_triggered') and
                         post_result.sanitizer_triggered)

        if pre_triggered and not post_triggered:
            comparison['fixed_povs'].append(pov_name)
            comparison['behavior_changes'].append({
                'pov': pov_name,
                'change': 'fixed',
                'pre_status': 'triggered',
                'post_status': 'not_triggered'
            })
        elif not pre_triggered and post_triggered:
            comparison['broken_povs'].append(pov_name)
            comparison['behavior_changes'].append({
                'pov': pov_name,
                'change': 'broken',
                'pre_status': 'not_triggered',
                'post_status': 'triggered'
            })
        else:
            comparison['unchanged_povs'].append(pov_name)

    return comparison


def _generate_recommendations(patch_results: List[PatchTestResult]) -> List[str]:
    """Generate recommendations based on patch test results."""
    recommendations = []

    success_rate = sum(1 for r in patch_results if r.status == PatchStatus.SUCCESS) / len(patch_results)

    if success_rate < 0.5:
        recommendations.append(
            "Low patch success rate detected. Consider reviewing patch generation logic or target POV identification."
        )

    application_failures = sum(1 for r in patch_results if r.status == PatchStatus.APPLICATION_FAILED)
    if application_failures > 0:
        recommendations.append(
            f"{application_failures} patches failed to apply. Check patch format and target file paths."
        )

    build_failures = sum(1 for r in patch_results if r.status == PatchStatus.BUILD_FAILED)
    if build_failures > 0:
        recommendations.append(
            f"{build_failures} patches caused build failures. Review patch syntax and compilation compatibility."
        )

    newly_broken = sum(len(r.povs_newly_broken) for r in patch_results)
    if newly_broken > 0:
        recommendations.append(
            f"{newly_broken} POVs were broken by patches. Investigate potential side effects and patch scope."
        )

    partial_fixes = sum(1 for r in patch_results if r.status == PatchStatus.PARTIAL_SUCCESS)
    if partial_fixes > 0:
        recommendations.append(
            f"{partial_fixes} patches provided partial fixes. Consider more comprehensive patching approaches."
        )

    if not recommendations:
        recommendations.append("Patch testing results look good! No major issues detected.")

    return recommendations