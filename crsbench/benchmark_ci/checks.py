"""Result checking functions for CI validation.

These functions validate result JSON files from verify, patch-verify,
and coverage commands.

Usage:
    from crsbench.benchmark_ci.checks import check_verify, check_patch_verify, check_coverage

CLI usage (backwards compatible):
    python -m crsbench.benchmark_ci.checks verify <benchmark_path> <results_json>
    python -m crsbench.benchmark_ci.checks patch-verify <results_json>
    python -m crsbench.benchmark_ci.checks coverage <results_json>
"""

import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def get_expected_cpvs(
    benchmark_path: Path,
    harness: Optional[str] = None,
) -> set[str]:
    """Get expected CPVs from benchmark meta.yaml.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Optional harness filter

    Returns:
        Set of expected CPV keywords
    """
    meta_path = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_path.exists():
        logger.error(f"meta.yaml not found: {meta_path}")
        return set()

    with meta_path.open() as f:
        meta = yaml.safe_load(f)

    cpvs = set()
    for harness_info in meta.get("harness_files", []):
        if harness is not None and harness_info.get("name") != harness:
            continue
        for vuln in harness_info.get("vulns", []):
            cpv = vuln.get("vuln_keyword")
            if cpv:
                cpvs.add(cpv)

    return cpvs


def check_verify(
    benchmark_path: Path,
    results_file: Path,
    harness: Optional[str] = None,
) -> bool:
    """Check verify results - all expected CPVs must be found.

    Args:
        benchmark_path: Path to benchmark directory
        results_file: Path to verification results JSON
        harness: Optional harness filter

    Returns:
        True if all expected CPVs were found
    """
    expected_cpvs = get_expected_cpvs(benchmark_path, harness=harness)
    logger.debug(f"Expected CPVs: {sorted(expected_cpvs)}")

    if not expected_cpvs:
        logger.error("No CPVs found in meta.yaml")
        return False

    with results_file.open() as f:
        results: list[dict[str, Any]] = json.load(f)

    if not results:
        logger.error("No results in file")
        return False

    # Collect found CPVs
    found_cpvs: set[str] = set()
    for r in results:
        found_cpvs.update(r.get("cpv_matched", []))

    logger.debug(f"Found CPVs: {sorted(found_cpvs)}")

    missing = expected_cpvs - found_cpvs
    if missing:
        logger.error(f"Missing CPVs: {sorted(missing)}")
        return False

    logger.debug("All expected CPVs found!")
    return True


def check_patch_verify(results_file: Path) -> bool:
    """Check patch-verify results - all must have pov_id and security_verdict=PASS.

    Args:
        results_file: Path to patch verification results JSON

    Returns:
        True if all patches passed security verification
    """
    with results_file.open() as f:
        data = json.load(f)

    # Handle both list format and dict with "results" key
    if isinstance(data, list):
        results: list[dict[str, Any]] = data
    else:
        results = data.get("results", [])

    if not results:
        logger.error("No results found")
        return False

    logger.debug(f"Total patches: {len(results)}")

    all_pass = True
    for r in results:
        pov_id = r.get("pov_id", "unknown")
        patch_id = r.get("patch_id", "unknown")
        security_verdict = r.get("security_verdict", "unknown")
        status = r.get("status", "unknown")

        if not pov_id or pov_id == "unknown":
            logger.error(f"  {patch_id} - missing pov_id")
            all_pass = False
            continue

        if security_verdict != "PASS":
            logger.error(
                f"  FAIL: {patch_id} ({pov_id}) - verdict={security_verdict}, status={status}"
            )
            all_pass = False
        else:
            logger.debug(f"  PASS: {patch_id} ({pov_id})")

    if all_pass:
        logger.debug("All patches passed!")
    else:
        logger.error("Some patches failed")

    return all_pass


def check_coverage(results_file: Path) -> bool:
    """Check coverage results - must have non-zero coverage.

    Args:
        results_file: Path to coverage results JSON

    Returns:
        True if coverage has non-zero lines covered
    """
    with results_file.open() as f:
        data = json.load(f)

    summary = data.get("summary", {})
    harness = data.get("harness", "unknown")

    lines_covered = summary.get("lines_covered", 0)
    lines_total = summary.get("lines_total", 0)
    lines_percent = summary.get("lines_percent", 0.0)
    totals_available = summary.get("totals_available")
    if totals_available is None:
        totals_available = lines_total > 0

    logger.debug(f"Harness: {harness}")
    if totals_available:
        logger.debug(
            f"Lines covered: {lines_covered}/{lines_total} ({lines_percent:.1f}%)"
        )
        if lines_total == 0:
            logger.error("Coverage report marked totals as available but lines_total=0")
            return False
    else:
        if lines_total > 0 or lines_percent > 0.0:
            logger.error(
                "Coverage report marked totals as unavailable but still populated "
                "line denominators"
            )
            return False
        logger.debug(f"Lines covered: {lines_covered} (total N/A)")

    if lines_covered == 0:
        logger.error("No lines covered")
        return False

    logger.debug("Coverage check passed!")
    return True


def main() -> int:
    """CLI entry point for backwards compatibility."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <verify|patch-verify|coverage> [args...]")  # noqa: T201
        return 1

    command = sys.argv[1]

    if command == "verify":
        harness = None
        if len(sys.argv) == 6 and sys.argv[4] == "--harness":
            harness = sys.argv[5]
        elif len(sys.argv) != 4:
            print(  # noqa: T201
                f"Usage: {sys.argv[0]} verify <benchmark_path> <results_json> [--harness <name>]"
            )
            return 1
        benchmark_path = Path(sys.argv[2])
        results_file = Path(sys.argv[3])
        if not benchmark_path.exists():
            print(f"ERROR: Benchmark not found: {benchmark_path}")  # noqa: T201
            return 1
        if not results_file.exists():
            print(f"ERROR: Results file not found: {results_file}")  # noqa: T201
            return 1
        return 0 if check_verify(benchmark_path, results_file, harness=harness) else 1

    if command == "patch-verify":
        if len(sys.argv) != 3:
            print(f"Usage: {sys.argv[0]} patch-verify <results_json>")  # noqa: T201
            return 1
        results_file = Path(sys.argv[2])
        if not results_file.exists():
            print(f"ERROR: Results file not found: {results_file}")  # noqa: T201
            return 1
        return 0 if check_patch_verify(results_file) else 1

    if command == "coverage":
        if len(sys.argv) != 3:
            print(f"Usage: {sys.argv[0]} coverage <results_json>")  # noqa: T201
            return 1
        results_file = Path(sys.argv[2])
        if not results_file.exists():
            print(f"ERROR: Results file not found: {results_file}")  # noqa: T201
            return 1
        return 0 if check_coverage(results_file) else 1

    print(f"Unknown command: {command}")  # noqa: T201
    print(f"Usage: {sys.argv[0]} <verify|patch-verify|coverage> [args...]")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
