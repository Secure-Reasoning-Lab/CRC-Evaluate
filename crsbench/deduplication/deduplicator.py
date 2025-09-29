"""Main POV deduplication orchestrator."""

from typing import List, Dict, Optional
from dataclasses import dataclass
from crsbench.evaluation.results import POVResult, HarnessResult, EvaluationReport
from .analyzer import RootCauseAnalyzer, MultiSanitizerAnalyzer, RootCause
from .strategies import DeduplicationStrategy, HybridStrategy, DeduplicationGroup


@dataclass
class DeduplicationResult:
    """Result of POV deduplication process."""
    original_pov_count: int
    deduplicated_pov_count: int
    deduplication_groups: List[DeduplicationGroup]
    root_causes: Dict[str, RootCause]

    @property
    def deduplication_rate(self) -> float:
        """Calculate the deduplication rate."""
        if self.original_pov_count == 0:
            return 0.0
        return 1.0 - (self.deduplicated_pov_count / self.original_pov_count)

    def get_representative_povs(self) -> List[str]:
        """Get list of representative POVs after deduplication."""
        return [group.representative_pov for group in self.deduplication_groups]

    def get_duplicates_for_pov(self, pov_name: str) -> List[str]:
        """Get list of POVs that are duplicates of the given POV."""
        for group in self.deduplication_groups:
            if pov_name in group.pov_names:
                # Return all POVs in group except the given one
                return [p for p in group.pov_names if p != pov_name]
        return []


class POVDeduplicator:
    """Main class for POV deduplication."""

    def __init__(self,
                 analyzer: Optional[RootCauseAnalyzer] = None,
                 strategy: Optional[DeduplicationStrategy] = None):
        """Initialize POV deduplicator.

        Args:
            analyzer: Root cause analyzer (defaults to MultiSanitizerAnalyzer)
            strategy: Deduplication strategy (defaults to HybridStrategy)
        """
        self.analyzer = analyzer or MultiSanitizerAnalyzer()
        self.strategy = strategy or HybridStrategy()

    def deduplicate_povs(self, pov_results: List[POVResult]) -> DeduplicationResult:
        """Deduplicate POVs based on root cause analysis.

        Args:
            pov_results: List of POV results from evaluation

        Returns:
            DeduplicationResult with grouped POVs
        """
        # Filter to only found POVs
        found_povs = [pov for pov in pov_results if pov.status.value == "found"]

        if not found_povs:
            return DeduplicationResult(
                original_pov_count=0,
                deduplicated_pov_count=0,
                deduplication_groups=[],
                root_causes={}
            )

        # Analyze root causes
        root_causes = {}
        for pov in found_povs:
            root_cause = self._analyze_pov_root_cause(pov)
            if root_cause:
                root_causes[pov.name] = root_cause

        # Apply deduplication strategy
        groups = self.strategy.deduplicate(root_causes)

        return DeduplicationResult(
            original_pov_count=len(found_povs),
            deduplicated_pov_count=len(groups),
            deduplication_groups=groups,
            root_causes=root_causes
        )

    def deduplicate_harness_results(self, harness_results: List[HarnessResult]) -> DeduplicationResult:
        """Deduplicate POVs across multiple harness results.

        Args:
            harness_results: List of harness results from evaluation

        Returns:
            DeduplicationResult with globally deduplicated POVs
        """
        # Collect all POV results
        all_povs = []
        for harness_result in harness_results:
            all_povs.extend(harness_result.pov_results)

        return self.deduplicate_povs(all_povs)

    def _analyze_pov_root_cause(self, pov_result: POVResult) -> Optional[RootCause]:
        """Analyze root cause for a single POV result."""
        if not pov_result.crs_output:
            return None

        return self.analyzer.analyze_pov(
            pov_output=pov_result.crs_output,
            sanitizer=pov_result.sanitizer,
            error_token=pov_result.error_token
        )


class DeduplicationReporter:
    """Generates reports for deduplication results."""

    @staticmethod
    def generate_summary(result: DeduplicationResult) -> str:
        """Generate a text summary of deduplication results."""
        if result.original_pov_count == 0:
            return "No POVs found to deduplicate."

        lines = [
            "POV Deduplication Summary",
            "=" * 40,
            f"Original POVs: {result.original_pov_count}",
            f"Unique root causes: {result.deduplicated_pov_count}",
            f"Deduplication rate: {result.deduplication_rate:.1%}",
            ""
        ]

        # Show groups with multiple POVs
        duplicates = [g for g in result.deduplication_groups if len(g.pov_names) > 1]
        if duplicates:
            lines.append("Duplicate Groups:")
            for i, group in enumerate(duplicates, 1):
                lines.append(f"  {i}. {group.vulnerability_type.value} at {group.root_cause.source_location}")
                lines.append(f"     Representative: {group.representative_pov}")
                lines.append(f"     Duplicates: {', '.join([p for p in group.pov_names if p != group.representative_pov])}")
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def generate_detailed_report(result: DeduplicationResult) -> Dict:
        """Generate detailed JSON-serializable report."""
        return {
            "summary": {
                "original_pov_count": result.original_pov_count,
                "deduplicated_pov_count": result.deduplicated_pov_count,
                "deduplication_rate": result.deduplication_rate
            },
            "groups": [
                {
                    "vulnerability_type": group.root_cause.vulnerability_type.value,
                    "source_location": group.root_cause.source_location,
                    "representative_pov": group.representative_pov,
                    "duplicate_povs": [p for p in group.pov_names if p != group.representative_pov],
                    "confidence": group.root_cause.confidence
                }
                for group in result.deduplication_groups
            ],
            "root_causes": {
                pov_name: {
                    "vulnerability_type": cause.vulnerability_type.value,
                    "source_location": cause.source_location,
                    "error_signature": cause.error_signature,
                    "confidence": cause.confidence
                }
                for pov_name, cause in result.root_causes.items()
            }
        }