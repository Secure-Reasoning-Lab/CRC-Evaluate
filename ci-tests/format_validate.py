#!/usr/bin/env python3
"""Validate benchmark and experiment config formats.

Usage:
    python format_validate.py benchmarks/              # validate all benchmarks
    python format_validate.py benchmarks/mock-c        # validate single benchmark
    python format_validate.py --experiment-config config.yaml
    python format_validate.py benchmarks/ --verbose

Examples:
    python format_validate.py benchmarks/
    python format_validate.py benchmarks/sanity-mock-c-delta-01 --verbose
    python format_validate.py --experiment-config experiment-configs/e2e-test.yaml
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from crsbench.validation import (
    validate_benchmark,
    validate_experiment_config,
)


def format_status(is_valid: bool, *, use_color: bool = True) -> str:
    """Format status with optional color."""
    if not use_color:
        return "PASS" if is_valid else "FAIL"
    if is_valid:
        return "\033[92mPASS\033[0m"
    return "\033[91mFAIL\033[0m"


def validate_single_benchmark(
    path: Path, *, verbose: bool = False, use_color: bool = True
) -> bool:
    """Validate a single benchmark and print result."""
    result = validate_benchmark(path)
    status = format_status(result.is_valid, use_color=use_color)
    print(f"  {path.name}: {status}")

    if verbose or not result.is_valid:
        for error in result.errors:
            print(f"    ERROR: {error.message}")
            if error.field:
                print(f"           Field: {error.field}")

        if verbose:
            for warning in result.warnings:
                print(f"    WARN: {warning.message}")

    return result.is_valid


def validate_benchmarks(
    path: Path, *, verbose: bool = False, use_color: bool = True
) -> tuple[int, int]:
    """Validate benchmarks at given path.

    Returns:
        Tuple of (passed_count, failed_count)
    """
    passed = 0
    failed = 0

    if path.is_file():
        # Single meta.yaml file
        if validate_single_benchmark(path.parent, verbose=verbose, use_color=use_color):
            passed += 1
        else:
            failed += 1
    elif (path / ".aixcc").exists():
        # Single benchmark directory
        if validate_single_benchmark(path, verbose=verbose, use_color=use_color):
            passed += 1
        else:
            failed += 1
    else:
        # Directory containing multiple benchmarks
        benchmark_dirs = sorted(
            [d for d in path.iterdir() if d.is_dir() and (d / ".aixcc").exists()]
        )
        if not benchmark_dirs:
            print(f"No benchmarks found in {path}")
            return 0, 0

        for benchmark in benchmark_dirs:
            if validate_single_benchmark(
                benchmark, verbose=verbose, use_color=use_color
            ):
                passed += 1
            else:
                failed += 1

    return passed, failed


def validate_experiment(
    path: Path, *, verbose: bool = False, use_color: bool = True
) -> bool:
    """Validate experiment config and print result."""
    result = validate_experiment_config(path)
    status = format_status(result.is_valid, use_color=use_color)
    print(f"  {path.name}: {status}")

    if verbose or not result.is_valid:
        for error in result.errors:
            print(f"    ERROR: {error.message}")
            if error.field:
                print(f"           Field: {error.field}")

        if verbose:
            for warning in result.warnings:
                print(f"    WARN: {warning.message}")

    return result.is_valid


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate benchmark/config formats")
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help="Path to benchmark(s) directory or meta.yaml file",
    )
    parser.add_argument(
        "--experiment-config",
        type=Path,
        help="Path to experiment config YAML file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed validation output including warnings",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    args = parser.parse_args()

    use_color = not args.no_color

    # Validate experiment config if specified
    if args.experiment_config:
        if not args.experiment_config.exists():
            print(f"ERROR: Experiment config not found: {args.experiment_config}")
            return 1

        print("Validating experiment config:")
        if validate_experiment(
            args.experiment_config, verbose=args.verbose, use_color=use_color
        ):
            print("\nExperiment config validation passed")
            return 0
        print("\nExperiment config validation failed")
        return 1

    # Validate benchmarks
    if not args.path:
        parser.error("Either path or --experiment-config is required")

    if not args.path.exists():
        print(f"ERROR: Path not found: {args.path}")
        return 1

    print("Validating benchmarks:")
    passed, failed = validate_benchmarks(
        args.path, verbose=args.verbose, use_color=use_color
    )

    print(f"\nSummary: {passed} passed, {failed} failed")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
