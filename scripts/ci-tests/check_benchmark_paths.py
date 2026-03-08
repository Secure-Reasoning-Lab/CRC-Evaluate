#!/usr/bin/env python3
"""Fail CI when benchmarks contain legacy hard-coded source roots."""

from pathlib import Path

from crsbench.benchmark.packaging.path_policy import (
    find_path_policy_violations,
    format_path_policy_error,
)


def main() -> int:
    root = Path("benchmarks")
    if not root.exists():
        print("benchmarks/ directory not found")
        return 1

    benchmark_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    failures: list[str] = []
    for benchmark_dir in benchmark_dirs:
        violations = find_path_policy_violations(benchmark_dir)
        if violations:
            failures.append(format_path_policy_error(benchmark_dir, violations))

    if failures:
        print("Benchmark path policy check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Benchmark path policy check passed ({len(benchmark_dirs)} benchmarks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
