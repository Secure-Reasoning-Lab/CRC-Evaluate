"""Deduplication strategies for POV verification results.

This module provides an extensible framework for deduplicating POVs
based on different criteria. Initial implementation uses patch-based
deduplication (POVs triggering the same CPV set are duplicates).

Future extensions can add:
- Stack-based deduplication (comparing crash stack traces)
- Hybrid deduplication (combining multiple strategies)
- Content-based deduplication (comparing POV content)
"""

from abc import ABC, abstractmethod

from crsbench.evaluation.verification.models import (
    VerificationResult,
    VerificationStatus,
)


class DeduplicationStrategy(ABC):
    """Abstract base class for POV deduplication strategies.

    Implementations should define how to identify duplicate POVs
    based on their verification results.
    """

    @abstractmethod
    def deduplicate(
        self, results: list[VerificationResult]
    ) -> list[VerificationResult]:
        """Remove duplicate POVs from results.

        Args:
            results: List of verification results to deduplicate

        Returns:
            List of unique verification results
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of this deduplication strategy."""


class PatchBasedDedup(DeduplicationStrategy):
    """Deduplicate POVs by their CPV match set.

    Two POVs are considered duplicates if they trigger the exact same
    set of CPVs. This is the simplest form of deduplication based on
    the patch/vulnerability that each POV exposes.

    Example:
        - POV1 triggers [cpv_0, cpv_1]
        - POV2 triggers [cpv_0, cpv_1]  <- duplicate of POV1
        - POV3 triggers [cpv_0]          <- unique
        - POV4 triggers [cpv_2]          <- unique
    """

    @property
    def name(self) -> str:
        return "patch-based"

    def deduplicate(
        self, results: list[VerificationResult]
    ) -> list[VerificationResult]:
        """Deduplicate results by CPV match set.

        Only results with status CPV are deduplicated based on their
        cpv_matched list. Other statuses (ZERODAY, NOT_VULNERABLE, etc.)
        are kept as-is since they represent different outcomes.

        Args:
            results: List of verification results

        Returns:
            Deduplicated list, keeping first occurrence of each CPV set
        """
        unique_results = []
        seen_cpv_sets: set[tuple[str, ...]] = set()

        for result in results:
            # Only deduplicate CPV results
            if result.status == VerificationStatus.CPV:
                # Create a hashable key from the CPV set
                cpv_key = tuple(sorted(result.cpv_matched))

                if cpv_key in seen_cpv_sets:
                    continue  # Skip duplicate

                seen_cpv_sets.add(cpv_key)

            unique_results.append(result)

        return unique_results


class NoOpDedup(DeduplicationStrategy):
    """No-op deduplication strategy that keeps all results.

    Useful for debugging or when deduplication is not desired.
    """

    @property
    def name(self) -> str:
        return "none"

    def deduplicate(
        self, results: list[VerificationResult]
    ) -> list[VerificationResult]:
        """Return all results without modification."""
        return list(results)


class StatusBasedDedup(DeduplicationStrategy):
    """Deduplicate by keeping only one result per status type.

    This is a simple strategy that keeps only the first POV for each
    unique status (CPV, ZERODAY, etc.). Useful for getting a summary
    of what types of vulnerabilities were found.
    """

    @property
    def name(self) -> str:
        return "status-based"

    def deduplicate(
        self, results: list[VerificationResult]
    ) -> list[VerificationResult]:
        """Keep only one result per status type.

        For CPV status, further deduplicates by CPV set.
        """
        unique_results = []
        seen_statuses: set[VerificationStatus] = set()
        seen_cpv_sets: set[tuple[str, ...]] = set()

        for result in results:
            if result.status == VerificationStatus.CPV:
                cpv_key = tuple(sorted(result.cpv_matched))
                if cpv_key in seen_cpv_sets:
                    continue
                seen_cpv_sets.add(cpv_key)
            else:
                if result.status in seen_statuses:
                    continue
                seen_statuses.add(result.status)

            unique_results.append(result)

        return unique_results


def get_dedup_strategy(name: str) -> DeduplicationStrategy:
    """Get a deduplication strategy by name.

    Args:
        name: Strategy name ("patch-based", "none", "status-based")

    Returns:
        DeduplicationStrategy instance

    Raises:
        ValueError: If strategy name is unknown
    """
    strategies: dict[str, type[DeduplicationStrategy]] = {
        "patch-based": PatchBasedDedup,
        "none": NoOpDedup,
        "status-based": StatusBasedDedup,
    }

    if name not in strategies:
        available = ", ".join(strategies.keys())
        raise ValueError(f"Unknown dedup strategy: {name}. Available: {available}")

    return strategies[name]()
