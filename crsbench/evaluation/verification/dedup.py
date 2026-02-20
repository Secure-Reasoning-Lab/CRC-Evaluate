"""Deduplication strategies for POV verification results.

This module provides an extensible framework for deduplicating POVs
based on different criteria:
- PatchBasedDedup: POVs triggering the same CPV set are duplicates
- StackBasedDedup: Groups UNINTENDED_CRASH POVs by crash signature hash
- StatusBasedDedup: Keeps one result per status type
- NoOpDedup: No deduplication (debug/testing)
"""

from abc import ABC, abstractmethod
from typing import Optional

from crsbench.evaluation.verification.models import (
    PatchVerificationResult,
    PatchVerificationStatus,
    PovVerificationResult,
    PovVerificationStatus,
)


class DeduplicationStrategy(ABC):
    """Abstract base class for POV deduplication strategies.

    Implementations should define how to identify duplicate POVs
    based on their verification results.
    """

    @abstractmethod
    def deduplicate(
        self, results: list[PovVerificationResult]
    ) -> list[PovVerificationResult]:
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
        self, results: list[PovVerificationResult]
    ) -> list[PovVerificationResult]:
        """Deduplicate results by CPV match set.

        Only results with status CPV are deduplicated based on their
        cpv_matched list. Other statuses (NOT_VULNERABLE, UNINTENDED_CRASH, etc.)
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
            if result.status == PovVerificationStatus.CPV:
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
        self, results: list[PovVerificationResult]
    ) -> list[PovVerificationResult]:
        """Return all results without modification."""
        return list(results)


class StatusBasedDedup(DeduplicationStrategy):
    """Deduplicate by keeping only one result per status type.

    This is a simple strategy that keeps only the first POV for each
    unique status (CPV, NOT_VULNERABLE, etc.). Useful for getting a summary
    of what types of results were found.
    """

    @property
    def name(self) -> str:
        return "status-based"

    def deduplicate(
        self, results: list[PovVerificationResult]
    ) -> list[PovVerificationResult]:
        """Keep only one result per status type.

        For CPV status, further deduplicates by CPV set.
        """
        unique_results = []
        seen_statuses: set[PovVerificationStatus] = set()
        seen_cpv_sets: set[tuple[str, ...]] = set()

        for result in results:
            if result.status == PovVerificationStatus.CPV:
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


class StackBasedDedup(DeduplicationStrategy):
    """Deduplicate UNINTENDED_CRASH POVs by crash signature hash.

    Uses sanitizer stack trace parsing to compute a deterministic crash
    signature for each unintended crash. POVs with the same crash site
    (same signature hash) are considered duplicates.

    For CPV results, falls back to PatchBasedDedup behavior (dedup by
    cpv_matched set). Other statuses are kept as-is.
    """

    def __init__(self, *, top_n: int = 5) -> None:
        self._top_n = top_n

    @property
    def name(self) -> str:
        return "stack-based"

    def _extract_crash_log(self, result: PovVerificationResult) -> Optional[str]:
        """Extract the first available crash log from a verification result.

        Looks in crash_info for stdout logs (per-variant crash output).

        Args:
            result: Verification result to extract crash log from

        Returns:
            First crash log string, or None if not available
        """
        if not result.crash_info:
            return None

        # Try stdout logs (standard crash_info structure from engine)
        stdout_logs = result.crash_info.get("stdout")
        if stdout_logs and isinstance(stdout_logs, dict):
            for crash_log in stdout_logs.values():
                if crash_log:
                    return crash_log

        # Try logs key (from verify_pov single-POV path)
        logs = result.crash_info.get("logs")
        if logs and isinstance(logs, dict):
            for crash_log in logs.values():
                if crash_log:
                    return crash_log

        return None

    def deduplicate(
        self, results: list[PovVerificationResult]
    ) -> list[PovVerificationResult]:
        """Deduplicate results using crash signature for unintended crashes.

        - CPV results: deduplicated by cpv_matched set (same as PatchBasedDedup)
        - UNINTENDED_CRASH results: deduplicated by crash signature hash
        - Other statuses: kept as-is

        Args:
            results: List of verification results

        Returns:
            Deduplicated list
        """
        from crsbench.evaluation.verification.crash_signature import (
            parse_crash_signature,
        )

        unique_results: list[PovVerificationResult] = []
        seen_cpv_sets: set[tuple[str, ...]] = set()
        seen_crash_signatures: set[str] = set()

        for result in results:
            if result.status == PovVerificationStatus.CPV:
                cpv_key = tuple(sorted(result.cpv_matched))
                if cpv_key in seen_cpv_sets:
                    continue
                seen_cpv_sets.add(cpv_key)

            elif result.status == PovVerificationStatus.UNINTENDED_CRASH:
                # Use pre-computed signature if available, else parse from logs
                sig_hash = result.crash_signature
                if not sig_hash:
                    crash_log = self._extract_crash_log(result)
                    if crash_log:
                        signature = parse_crash_signature(crash_log, top_n=self._top_n)
                        if signature:
                            sig_hash = signature.signature_hash

                if sig_hash:
                    if sig_hash in seen_crash_signatures:
                        continue
                    seen_crash_signatures.add(sig_hash)
                # If no signature found, keep the result

            unique_results.append(result)

        return unique_results


# =============================================================================
# Patch Verification Deduplication Strategies
# =============================================================================


class PatchDeduplicationStrategy(ABC):
    """Abstract base class for patch verification deduplication strategies.

    Implementations should define how to identify duplicate patches
    based on their verification results.
    """

    @abstractmethod
    def deduplicate(
        self, results: list[PatchVerificationResult]
    ) -> list[PatchVerificationResult]:
        """Remove duplicate patches from results.

        Args:
            results: List of patch verification results to deduplicate

        Returns:
            List of unique patch verification results
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of this deduplication strategy."""


class CpvFixedDedup(PatchDeduplicationStrategy):
    """Deduplicate patches by cpv_fixed fingerprint.

    Two patches are considered duplicates if they fix the exact same
    set of CPVs. This allows identifying functionally equivalent patches.

    Example:
        - Patch1 fixes [cpv_0, cpv_1]
        - Patch2 fixes [cpv_0, cpv_1]  <- duplicate of Patch1
        - Patch3 fixes [cpv_0]          <- unique
        - Patch4 fixes []               <- unique (fixes nothing)
    """

    @property
    def name(self) -> str:
        return "cpv-fixed"

    def deduplicate(
        self, results: list[PatchVerificationResult]
    ) -> list[PatchVerificationResult]:
        """Deduplicate patches by their cpv_fixed fingerprint.

        Only VALID patches are deduplicated based on their cpv_fixed list.
        Other statuses (BUILD_FAILED, TEST_FAILED, etc.) are kept as-is.

        Args:
            results: List of patch verification results

        Returns:
            Deduplicated list, keeping first occurrence of each cpv_fixed set
        """
        unique_results: list[PatchVerificationResult] = []
        seen_cpv_sets: set[tuple[str, ...]] = set()

        for result in results:
            # Only deduplicate VALID patches with cpv_fixed
            if result.status == PatchVerificationStatus.VALID and result.cpv_fixed:
                cpv_key = tuple(sorted(result.cpv_fixed))

                if cpv_key in seen_cpv_sets:
                    continue  # Skip duplicate

                seen_cpv_sets.add(cpv_key)

            unique_results.append(result)

        return unique_results


class NoOpPatchDedup(PatchDeduplicationStrategy):
    """No-op deduplication strategy that keeps all patch results."""

    @property
    def name(self) -> str:
        return "none"

    def deduplicate(
        self, results: list[PatchVerificationResult]
    ) -> list[PatchVerificationResult]:
        """Return all results without modification."""
        return list(results)


def get_dedup_strategy(name: str, *, top_n: int = 5) -> DeduplicationStrategy:
    """Get a deduplication strategy by name.

    Args:
        name: Strategy name ("patch-based", "none", "status-based", "stack-based")
        top_n: Stack frame depth for stack-based dedup (default: 5)

    Returns:
        DeduplicationStrategy instance

    Raises:
        ValueError: If strategy name is unknown
    """
    strategies: dict[str, type[DeduplicationStrategy]] = {
        "patch-based": PatchBasedDedup,
        "none": NoOpDedup,
        "status-based": StatusBasedDedup,
        "stack-based": StackBasedDedup,
    }

    if name not in strategies:
        available = ", ".join(strategies.keys())
        raise ValueError(f"Unknown dedup strategy: {name}. Available: {available}")

    if name == "stack-based":
        return StackBasedDedup(top_n=top_n)

    return strategies[name]()


def get_patch_dedup_strategy(name: str) -> PatchDeduplicationStrategy:
    """Get a patch deduplication strategy by name.

    Args:
        name: Strategy name ("cpv-fixed", "none")

    Returns:
        PatchDeduplicationStrategy instance

    Raises:
        ValueError: If strategy name is unknown
    """
    strategies: dict[str, type[PatchDeduplicationStrategy]] = {
        "cpv-fixed": CpvFixedDedup,
        "none": NoOpPatchDedup,
    }

    if name not in strategies:
        available = ", ".join(strategies.keys())
        raise ValueError(
            f"Unknown patch dedup strategy: {name}. Available: {available}"
        )

    return strategies[name]()
