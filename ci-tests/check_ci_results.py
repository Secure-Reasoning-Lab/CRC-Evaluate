#!/usr/bin/env python3
"""Check CI results for verify and patch-verify commands.

Usage:
    python check_ci_results.py verify <benchmark_path> <results_json>
    python check_ci_results.py patch-verify <results_json>

Examples:
    python check_ci_results.py verify benchmarks/sanity-mock-c-delta-01 verify-results.json
    python check_ci_results.py patch-verify patch-verify-results.json
"""

import json
import sys
from pathlib import Path

import yaml


def get_expected_cpvs(benchmark_path: Path) -> set[str]:
    """Get expected CPVs from benchmark meta.yaml."""
    meta_path = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_path.exists():
        print(f"ERROR: meta.yaml not found: {meta_path}")
        sys.exit(1)

    with open(meta_path) as f:
        meta = yaml.safe_load(f)

    cpvs = set()
    for harness in meta.get("harness_files", []):
        for vuln in harness.get("vulns", []):
            cpv = vuln.get("vuln_keyword")
            if cpv:
                cpvs.add(cpv)

    return cpvs


def check_verify(benchmark_path: Path, results_file: Path) -> bool:
    """Check verify results - all expected CPVs must be found."""
    expected_cpvs = get_expected_cpvs(benchmark_path)
    print(f"Expected CPVs: {sorted(expected_cpvs)}")

    if not expected_cpvs:
        print("ERROR: No CPVs found in meta.yaml")
        return False

    with open(results_file) as f:
        results = json.load(f)

    if not results:
        print("ERROR: No results in file")
        return False

    # Collect found CPVs
    found_cpvs = set()
    for r in results:
        found_cpvs.update(r.get("cpv_matched", []))

    print(f"Found CPVs: {sorted(found_cpvs)}")

    missing = expected_cpvs - found_cpvs
    if missing:
        print(f"ERROR: Missing CPVs: {sorted(missing)}")
        return False

    print("All expected CPVs found!")
    return True


def check_patch_verify(results_file: Path) -> bool:
    """Check patch-verify results - all must have pov_id and security_verdict=PASS."""
    with open(results_file) as f:
        data = json.load(f)

    # Handle both list format and dict with "results" key
    if isinstance(data, list):
        results = data
    else:
        results = data.get("results", [])

    if not results:
        print("ERROR: No results found")
        return False

    print(f"Total patches: {len(results)}")

    all_pass = True
    for r in results:
        pov_id = r.get("pov_id", "unknown")
        patch_id = r.get("patch_id", "unknown")
        security_verdict = r.get("security_verdict", "unknown")
        status = r.get("status", "unknown")

        if not pov_id or pov_id == "unknown":
            print(f"  ERROR: {patch_id} - missing pov_id")
            all_pass = False
            continue

        if security_verdict != "PASS":
            print(f"  FAIL: {patch_id} ({pov_id}) - verdict={security_verdict}, status={status}")
            all_pass = False
        else:
            print(f"  PASS: {patch_id} ({pov_id})")

    if all_pass:
        print("\nAll patches passed!")
    else:
        print("\nERROR: Some patches failed")

    return all_pass


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <verify|patch-verify> [args...]")
        return 1

    command = sys.argv[1]

    if command == "verify":
        if len(sys.argv) != 4:
            print(f"Usage: {sys.argv[0]} verify <benchmark_path> <results_json>")
            return 1
        benchmark_path = Path(sys.argv[2])
        results_file = Path(sys.argv[3])
        if not benchmark_path.exists():
            print(f"ERROR: Benchmark not found: {benchmark_path}")
            return 1
        if not results_file.exists():
            print(f"ERROR: Results file not found: {results_file}")
            return 1
        return 0 if check_verify(benchmark_path, results_file) else 1

    elif command == "patch-verify":
        if len(sys.argv) != 3:
            print(f"Usage: {sys.argv[0]} patch-verify <results_json>")
            return 1
        results_file = Path(sys.argv[2])
        if not results_file.exists():
            print(f"ERROR: Results file not found: {results_file}")
            return 1
        return 0 if check_patch_verify(results_file) else 1

    else:
        print(f"Unknown command: {command}")
        print(f"Usage: {sys.argv[0]} <verify|patch-verify> [args...]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
