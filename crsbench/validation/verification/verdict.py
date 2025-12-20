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

import logging
from typing import Dict, List

from crsbench.validation.variant.models import BenchmarkMode, BuildTag
from crsbench.validation.verification.models import (
    VerificationResult,
    VerificationStatus,
)

logger = logging.getLogger(__name__)


class VerdictResolver:
    """Resolver for POV verification verdicts.

    Determines the final verification status based on crash results
    across different variant builds.
    """

    @staticmethod
    def resolve(
        mode: BenchmarkMode,
        crash_results: Dict[BuildTag, bool],
        cpv_crash_map: Dict[int, bool],
        benchmark_name: str,
        pov_id: str = None,
    ) -> VerificationResult:
        """Resolve the verification verdict based on crash results.

        Args:
            mode: FULL or DELTA benchmark mode
            crash_results: Map of BuildTag -> crashed (True/False)
            cpv_crash_map: Map of CPV number -> crashed (True/False)
            benchmark_name: Name of the benchmark
            pov_id: Optional POV identifier

        Returns:
            VerificationResult with the determined status
        """
        if mode == BenchmarkMode.DELTA:
            return VerdictResolver._resolve_delta_mode(
                crash_results, cpv_crash_map, benchmark_name, pov_id
            )
        else:
            return VerdictResolver._resolve_full_mode(
                crash_results, cpv_crash_map, benchmark_name, pov_id
            )

    @staticmethod
    def _resolve_full_mode(
        crash_results: Dict[BuildTag, bool],
        cpv_crash_map: Dict[int, bool],
        benchmark_name: str,
        pov_id: str = None,
    ) -> VerificationResult:
        """Resolve verdict for FULL mode benchmarks.

        Logic:
        1. If base doesn't crash → NOT_VULNERABLE
        2. If allpatched crashes → UNINTENDED_CRASH
        3. If any cpvN crashes → CPV
        4. Else → ZERODAY
        """
        base_crashed = crash_results.get(BuildTag.FULL_BASE, False)

        # Check if POV triggers the vulnerability at all
        if not base_crashed:
            logger.info(f"[{benchmark_name}] NOT_VULNERABLE - base did not crash")
            return VerificationResult(
                status=VerificationStatus.NOT_VULNERABLE,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV does not crash on base version",
            )

        # Check if allpatched still crashes (unintended crash)
        allpatched_crashed = crash_results.get(BuildTag.ALL_PATCHED, True)
        if allpatched_crashed:
            logger.info(
                f"[{benchmark_name}] UNINTENDED_CRASH - allpatched still crashes"
            )
            return VerificationResult(
                status=VerificationStatus.UNINTENDED_CRASH,
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
            logger.info(f"[{benchmark_name}] CPV - matched {matched_cpvs}")
            return VerificationResult(
                status=VerificationStatus.CPV,
                benchmark=benchmark_name,
                cpv_matched=matched_cpvs,
                pov_id=pov_id,
                details=f"POV triggers {len(matched_cpvs)} known vulnerability(ies)",
            )

        # Crashes base but no CPV variant crashes - this is a zeroday
        logger.info(f"[{benchmark_name}] ZERODAY - crashes base but no CPV matched")
        return VerificationResult(
            status=VerificationStatus.ZERODAY,
            benchmark=benchmark_name,
            cpv_matched=[],
            pov_id=pov_id,
            details="POV triggers unknown vulnerability not covered by any CPV",
        )

    @staticmethod
    def _resolve_delta_mode(
        crash_results: Dict[BuildTag, bool],
        cpv_crash_map: Dict[int, bool],
        benchmark_name: str,
        pov_id: str = None,
    ) -> VerificationResult:
        """Resolve verdict for DELTA mode benchmarks.

        Logic:
        1. If base crashes → ZERODAY (vulnerability exists before changes)
        2. If ref doesn't crash → NOT_VULNERABLE
        3. If allpatched crashes → UNINTENDED_CRASH
        4. If any cpvN crashes → CPV
        5. Else → UNINTENDED_CRASH
        """
        base_crashed = crash_results.get(BuildTag.DELTA_BASE, False)

        # If base crashes, the bug exists before the delta - it's a zeroday
        if base_crashed:
            logger.info(
                f"[{benchmark_name}] ZERODAY - crashed on DELTA_BASE (pre-existing bug)"
            )
            return VerificationResult(
                status=VerificationStatus.ZERODAY,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV triggers bug that exists before delta changes",
            )

        # Check if POV crashes on ref (the vulnerable version)
        ref_crashed = crash_results.get(BuildTag.DELTA_REF, False)
        if not ref_crashed:
            logger.info(
                f"[{benchmark_name}] NOT_VULNERABLE - did not crash on DELTA_REF"
            )
            return VerificationResult(
                status=VerificationStatus.NOT_VULNERABLE,
                benchmark=benchmark_name,
                cpv_matched=[],
                pov_id=pov_id,
                details="POV does not crash on reference version",
            )

        # Check if allpatched still crashes
        allpatched_crashed = crash_results.get(BuildTag.ALL_PATCHED, True)
        if allpatched_crashed:
            logger.info(
                f"[{benchmark_name}] UNINTENDED_CRASH - allpatched still crashes"
            )
            return VerificationResult(
                status=VerificationStatus.UNINTENDED_CRASH,
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
            logger.info(f"[{benchmark_name}] CPV - matched {matched_cpvs}")
            return VerificationResult(
                status=VerificationStatus.CPV,
                benchmark=benchmark_name,
                cpv_matched=matched_cpvs,
                pov_id=pov_id,
                details=f"POV triggers {len(matched_cpvs)} known vulnerability(ies)",
            )

        # Crashes ref but no CPV variant crashes - unintended crash
        logger.info(
            f"[{benchmark_name}] UNINTENDED_CRASH - crashes ref but no CPV matched"
        )
        return VerificationResult(
            status=VerificationStatus.UNINTENDED_CRASH,
            benchmark=benchmark_name,
            cpv_matched=[],
            pov_id=pov_id,
            details="POV crashes ref but not fixed by any known patch",
        )
