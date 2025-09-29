"""Integration layer for deduplication with evaluation results."""

from typing import Optional
from crsbench.evaluation.results import EvaluationReport
from .deduplicator import POVDeduplicator, DeduplicationResult, DeduplicationReporter
from .analyzer import RootCauseAnalyzer
from .strategies import DeduplicationStrategy


def deduplicate_evaluation_results(
    evaluation_report: EvaluationReport,
    analyzer: Optional[RootCauseAnalyzer] = None,
    strategy: Optional[DeduplicationStrategy] = None,
    verbose: bool = False
) -> DeduplicationResult:
    """Deduplicate POVs in an evaluation report.

    Args:
        evaluation_report: Evaluation report containing POV results
        analyzer: Optional custom root cause analyzer
        strategy: Optional custom deduplication strategy
        verbose: Whether to print deduplication summary

    Returns:
        DeduplicationResult with deduplicated POVs
    """
    deduplicator = POVDeduplicator(analyzer=analyzer, strategy=strategy)

    # Deduplicate across all harness results
    result = deduplicator.deduplicate_harness_results(evaluation_report.harness_results)

    if verbose:
        summary = DeduplicationReporter.generate_summary(result)
        print(summary)

    return result


def create_deduplicated_report(
    original_report: EvaluationReport,
    deduplication_result: DeduplicationResult
) -> dict:
    """Create a modified evaluation report with deduplication information.

    Args:
        original_report: Original evaluation report
        deduplication_result: Results from deduplication

    Returns:
        Dictionary containing original report + deduplication data
    """
    # Convert original report to dict
    report_dict = original_report.to_dict()

    # Add deduplication section
    report_dict["deduplication"] = DeduplicationReporter.generate_detailed_report(deduplication_result)

    # Add summary with deduplicated counts
    report_dict["summary"] = {
        "original_povs": original_report.povs_found,
        "unique_povs": deduplication_result.deduplicated_pov_count,
        "duplicate_povs": original_report.povs_found - deduplication_result.deduplicated_pov_count,
        "deduplication_rate": deduplication_result.deduplication_rate
    }

    return report_dict


# Convenience functions for different deduplication approaches

def deduplicate_by_location(evaluation_report: EvaluationReport,
                          tolerance: int = 5) -> DeduplicationResult:
    """Deduplicate POVs based on source location proximity."""
    from .strategies import LocationBasedStrategy

    strategy = LocationBasedStrategy(location_tolerance=tolerance)
    return deduplicate_evaluation_results(evaluation_report, strategy=strategy)


def deduplicate_by_stack_trace(evaluation_report: EvaluationReport,
                              stack_depth: int = 3,
                              similarity_threshold: float = 0.7) -> DeduplicationResult:
    """Deduplicate POVs based on stack trace similarity."""
    from .strategies import StackTraceStrategy

    strategy = StackTraceStrategy(
        stack_depth=stack_depth,
        similarity_threshold=similarity_threshold
    )
    return deduplicate_evaluation_results(evaluation_report, strategy=strategy)


def deduplicate_exact_match(evaluation_report: EvaluationReport) -> DeduplicationResult:
    """Deduplicate POVs using exact root cause matching."""
    from .strategies import ExactMatchStrategy

    strategy = ExactMatchStrategy()
    return deduplicate_evaluation_results(evaluation_report, strategy=strategy)