"""VerdictResolver for POV verification results.

This module implements the verdict logic for determining whether a POV
triggers a known CPV, a zeroday, or is not vulnerable.

Verdict Logic:

FULL Mode:
- If base doesn't crash: NOT_VULNERABLE
- If allpatched crashes: UNINTENDED_CRASH
- If any cpvN crashes: CPV (matched to crashing variants)
- Else: ZERODAY

DELTA Mode:
- If base crashes: ZERODAY (bug exists in base, not induced by changes)
- If ref doesn't crash: NOT_VULNERABLE
- If allpatched crashes: UNINTENDED_CRASH
- If any cpvN crashes: CPV
- Else: UNINTENDED_CRASH (crashes on ref but not fixed by any patch)
"""

from typing import Optional, Union

from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.evaluation.verification.models import (
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Type alias for variant identifiers (supports both new and legacy types)
VariantKey = Union[VariantType, str]


class VerdictResolver:
    """Resolver for POV verification verdicts.

    Determines the final verification status based on crash results
    across different variant builds.
    """

    @staticmethod
    def resolve(
        mode: BenchmarkMode,
        crash_results: dict[VariantType, bool],
        cpv_crash_map: dict[int, bool],
        benchmark_name: str,
        pov_id: Optional[str] = None,
    ) -> PovVerificationResult:
        """Resolve the verification verdict based on crash results.

        Args:
            mode: FULL or DELTA benchmark mode
            crash_results: Map of VariantType -> crashed (True/False)
            cpv_crash_map: Map of CPV number -> crashed (True/False)
            benchmark_name: Name of the benchmark
            pov_id: Optional POV identifier

        Returns:
            PovVerificationResult with the determined status
        """
        if mode == BenchmarkMode.DELTA:
            return VerdictResolver._resolve_delta_mode(
                crash_results, cpv_crash_map, benchmark_name, pov_id
            )
        return VerdictResolver._resolve_full_mode(
            crash_results, cpv_crash_map, benchmark_name, pov_id
        )

    @staticmethod
    def _resolve_full_mode(
        crash_results: dict[VariantType, bool],
        cpv_crash_map: dict[int, bool],
        benchmark_name: str,
        pov_id: Optional[str] = None,
    ) -> PovVerificationResult:
        """Resolve verdict for FULL mode benchmarks.

        Logic:
        0. If any required variant missing -> ERROR
        1. If base doesn't crash -> NOT_VULNERABLE
        2. If allpatched crashes -> UNINTENDED_CRASH
        3. If any cpvN crashes -> CPV
        4. Else -> ZERODAY
        """
        # Check all required variants exist (build/reproduce succeeded)
        required_variants = [VariantType.FULL_BASE, VariantType.ALL_PATCHED]
        missing = [v.value for v in required_variants if v not in crash_results]

        if missing:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.error(
                f"{pov_prefix}[{benchmark_name}] ERROR - missing results for: {missing}"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.ERROR,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details=f"Build or reproduce failed for variants: {missing}",
            )

        # Now safe to access without defaults
        base_crashed = crash_results[VariantType.FULL_BASE]

        # Check if POV triggers the vulnerability at all
        if not base_crashed:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.debug(
                f"{pov_prefix}[{benchmark_name}] NOT_VULNERABLE - base did not crash"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.NOT_VULNERABLE,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV does not crash on base version",
            )

        # Check if allpatched still crashes (unintended crash)
        allpatched_crashed = crash_results[VariantType.ALL_PATCHED]
        if allpatched_crashed:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.warning(
                f"{pov_prefix}[{benchmark_name}] UNINTENDED_CRASH - allpatched still crashes"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV crashes even with all patches applied",
            )

        # Check which CPVs are triggered
        matched_cpvs = [
            f"cpv_{cpv_num}"
            for cpv_num, crashed in sorted(cpv_crash_map.items())
            if crashed
        ]

        if matched_cpvs:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.info(f"{pov_prefix}[{benchmark_name}] CPV - matched {matched_cpvs}")
            return PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark=benchmark_name,
                cpv_matched=matched_cpvs,
                pov_id=pov_id,
                details=f"POV triggers {len(matched_cpvs)} known vulnerability(ies)",
            )

        # Crashes base but no CPV variant crashes - this is a zeroday
        pov_prefix = f"[{pov_id}] " if pov_id else ""
        logger.warning(
            f"{pov_prefix}[{benchmark_name}] ZERODAY - crashes base but no CPV matched"
        )
        return PovVerificationResult(
            status=PovVerificationStatus.ZERODAY,
            benchmark=benchmark_name,
            cpv_matched=[],
            pov_id=pov_id,
            details="POV triggers unknown vulnerability not covered by any CPV",
        )

    @staticmethod
    def _resolve_delta_mode(
        crash_results: dict[VariantType, bool],
        cpv_crash_map: dict[int, bool],
        benchmark_name: str,
        pov_id: Optional[str] = None,
    ) -> PovVerificationResult:
        """Resolve verdict for DELTA mode benchmarks.

        Logic:
        0. If any required variant missing -> ERROR
        1. If base crashes -> ZERODAY (vulnerability exists before changes)
        2. If ref doesn't crash -> NOT_VULNERABLE
        3. If allpatched crashes -> UNINTENDED_CRASH
        4. If any cpvN crashes -> CPV
        5. Else -> UNINTENDED_CRASH
        """
        # Check all required variants exist (build/reproduce succeeded)
        required_variants = [
            VariantType.DELTA_BASE,
            VariantType.DELTA_REF,
            VariantType.ALL_PATCHED,
        ]
        missing = [v.value for v in required_variants if v not in crash_results]

        if missing:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.error(
                f"{pov_prefix}[{benchmark_name}] ERROR - missing results for: {missing}"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.ERROR,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details=f"Build or reproduce failed for variants: {missing}",
            )

        # Now safe to access without defaults
        base_crashed = crash_results[VariantType.DELTA_BASE]

        # If base crashes, the bug exists before the delta - it's a zeroday
        if base_crashed:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.warning(
                f"{pov_prefix}[{benchmark_name}] ZERODAY - crashed on DELTA_BASE (pre-existing bug)"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.ZERODAY,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV triggers bug that exists before delta changes",
            )

        # Check if POV crashes on ref (the vulnerable version)
        ref_crashed = crash_results[VariantType.DELTA_REF]
        if not ref_crashed:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.debug(
                f"{pov_prefix}[{benchmark_name}] NOT_VULNERABLE - did not crash on DELTA_REF"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.NOT_VULNERABLE,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV does not crash on reference version",
            )

        # Check if allpatched still crashes
        allpatched_crashed = crash_results[VariantType.ALL_PATCHED]
        if allpatched_crashed:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.warning(
                f"{pov_prefix}[{benchmark_name}] UNINTENDED_CRASH - allpatched still crashes"
            )
            return PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV crashes even with all patches applied",
            )

        # Check which CPVs are triggered
        matched_cpvs = [
            f"cpv_{cpv_num}"
            for cpv_num, crashed in sorted(cpv_crash_map.items())
            if crashed
        ]

        if matched_cpvs:
            pov_prefix = f"[{pov_id}] " if pov_id else ""
            logger.info(f"{pov_prefix}[{benchmark_name}] CPV - matched {matched_cpvs}")
            return PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark=benchmark_name,
                cpv_matched=matched_cpvs,
                pov_id=pov_id,
                details=f"POV triggers {len(matched_cpvs)} known vulnerability(ies)",
            )

        # Crashes ref but no CPV variant crashes - unintended crash
        pov_prefix = f"[{pov_id}] " if pov_id else ""
        logger.warning(
            f"{pov_prefix}[{benchmark_name}] UNINTENDED_CRASH - crashes ref but no CPV matched"
        )
        return PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark=benchmark_name,
            cpv_matched=[],
            pov_id=pov_id,
            details="POV crashes ref but not fixed by any known patch",
        )
