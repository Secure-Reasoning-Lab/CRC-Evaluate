#!/usr/bin/env python3
"""
Snapshot Generator Helper - Simplified interface for bench_snapgen

This utility script provides sensible defaults for generating benchmark snapshots,
requiring only the benchmark name as input.

Usage:
    # Generate snapshots for a benchmark
    python scripts/generate-snapshots.py afc-curl-delta-01

    # Custom output directory
    python scripts/generate-snapshots.py afc-curl-delta-01 --output /tmp/my-trial

    # Patch generation mode
    python scripts/generate-snapshots.py afc-curl-delta-01 --mode patch

    # Higher difficulty
    python scripts/generate-snapshots.py afc-curl-delta-01 --difficulty 3

    # With fault injection
    python scripts/generate-snapshots.py afc-curl-delta-01 --faults 0.1

    # Multiple trials
    python scripts/generate-snapshots.py afc-curl-delta-01 --trials 3

    # Quick test (30 min trial, 10 min snapshots)
    python scripts/generate-snapshots.py afc-curl-delta-01 --quick

Examples:
    # Basic usage with defaults
    $ python scripts/generate-snapshots.py afc-curl-delta-01
    # Output: /tmp/snapshots/afc-curl-delta-01/
    # Mode: bug-finding, Difficulty: 1, Duration: 2h, Period: 15min

    # Patch mode with higher difficulty
    $ python scripts/generate-snapshots.py atlanta-activemq-delta-01 --mode patch --difficulty 3

    # Quick test run
    $ python scripts/generate-snapshots.py afc-curl-delta-01 --quick
    # Duration: 30min, Period: 10min (3 snapshots)

    # Multiple trials with fault injection
    $ python scripts/generate-snapshots.py afc-curl-delta-01 --trials 5 --faults 0.15

    # All benchmarks in benchmarks/ directory
    $ python scripts/generate-snapshots.py --all

Default Settings:
    - Benchmarks root: ./benchmarks
    - Output directory: /tmp/snapshots/<benchmark-name>
    - Duration: 7200s (2 hours)
    - Snapshot period: 900s (15 minutes) → 8 snapshots
    - Mode: bug-finding
    - Difficulty: 1
    - Fault injection: 0.0 (disabled)
    - Trials: 1
"""

import argparse
import sys
from pathlib import Path


def auto_select_harness(benchmark_data) -> str:
    """Auto-select first harness with POVs.

    Args:
        benchmark_data: Loaded benchmark data

    Returns:
        Harness name

    Raises:
        ValueError: If no harness has POVs
    """
    for h in benchmark_data.meta.harness_files:
        harness_povs = [k for k in benchmark_data.povs if k[0] == h.name]
        if harness_povs:
            print(f"Auto-selected harness: {h.name} ({len(harness_povs)} POVs)")
            return h.name
    raise ValueError("No harness with POVs found in benchmark")


def main():
    """Main entry point for snapshot generator helper."""
    parser = argparse.ArgumentParser(
        description="Generate benchmark snapshots with sensible defaults",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )

    # Benchmark selection
    parser.add_argument(
        "benchmark",
        nargs="?",
        help="Benchmark name (e.g., afc-curl-delta-01)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate snapshots for all benchmarks in benchmarks/",
    )

    # Output
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output directory (default: /tmp/snapshots/<benchmark-name>)",
    )

    # Timing presets
    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument(
        "--quick",
        action="store_true",
        help="Quick test (30 min trial, 10 min snapshots = 3 snapshots)",
    )
    timing_group.add_argument(
        "--short",
        action="store_true",
        help="Short run (1 hour trial, 15 min snapshots = 4 snapshots)",
    )
    timing_group.add_argument(
        "--long",
        action="store_true",
        help="Long run (4 hours trial, 30 min snapshots = 8 snapshots)",
    )

    # Or custom timing
    parser.add_argument(
        "--duration",
        type=int,
        help="Trial duration in seconds (default: 7200 = 2 hours)",
    )
    parser.add_argument(
        "--period",
        type=int,
        help="Snapshot period in seconds (default: 900 = 15 min)",
    )

    # Mode and difficulty
    parser.add_argument(
        "--mode",
        "-m",
        choices=["bug", "patch", "bug-finding", "patch-generation"],
        default="bug",
        help="Generation mode: bug/patch (default: bug)",
    )
    parser.add_argument(
        "--difficulty",
        "-d",
        type=int,
        choices=[1, 2, 3, 4, 5],
        default=1,
        help="Difficulty level 1-5 (default: 1)",
    )

    # Fault injection
    parser.add_argument(
        "--faults",
        "-f",
        type=float,
        default=0.0,
        help="Fault injection rate 0.0-1.0 (default: 0.0)",
    )

    # Multiple trials
    parser.add_argument(
        "--trials",
        "-t",
        type=int,
        default=1,
        help="Number of trials to generate (default: 1)",
    )

    # Harness selection
    parser.add_argument(
        "--harness",
        type=str,
        help="Target harness name (default: auto-select first with POVs)",
    )

    # Benchmarks root
    parser.add_argument(
        "--benchmarks-root",
        type=Path,
        default=Path("benchmarks"),
        help="Benchmarks root directory (default: ./benchmarks)",
    )

    args = parser.parse_args()

    # Validate benchmark selection
    if not args.all and not args.benchmark:
        parser.error("Either provide a benchmark name or use --all")

    if args.all and args.benchmark:
        parser.error("Cannot use --all with a specific benchmark name")

    # Check benchmarks root exists
    if not args.benchmarks_root.exists():
        print(f"Error: Benchmarks directory not found: {args.benchmarks_root}")
        sys.exit(1)

    # Determine timing
    if args.quick:
        duration = 1800  # 30 minutes
        period = 600  # 10 minutes
    elif args.short:
        duration = 3600  # 1 hour
        period = 900  # 15 minutes
    elif args.long:
        duration = 14400  # 4 hours
        period = 1800  # 30 minutes
    else:
        duration = args.duration or 7200  # Default: 2 hours
        period = args.period or 900  # Default: 15 minutes

    # Normalize mode
    mode_map = {"bug": "bug-finding", "patch": "patch-generation"}
    mode = mode_map.get(args.mode, args.mode)

    # Get benchmarks to process
    if args.all:
        benchmarks = [
            b for b in args.benchmarks_root.iterdir()
            if b.is_dir() and (b / ".aixcc").exists()
        ]
        if not benchmarks:
            print(f"No benchmarks with .aixcc/ found in {args.benchmarks_root}")
            sys.exit(1)
        print(f"Found {len(benchmarks)} benchmarks to process")
    else:
        benchmark_path = args.benchmarks_root / args.benchmark
        if not benchmark_path.exists():
            print(f"Error: Benchmark not found: {benchmark_path}")
            sys.exit(1)
        if not (benchmark_path / ".aixcc").exists():
            print(f"Error: Benchmark missing .aixcc/ directory: {benchmark_path}")
            sys.exit(1)
        benchmarks = [benchmark_path]

    # Process each benchmark
    for benchmark_path in benchmarks:
        benchmark_name = benchmark_path.name

        # Determine output directory
        if args.output:
            if args.all:
                output_dir = args.output / benchmark_name
            else:
                output_dir = args.output
        else:
            output_dir = Path("/tmp/snapshots") / benchmark_name

        print(f"\n{'=' * 70}")
        print(f"Generating snapshots for: {benchmark_name}")
        print(f"{'=' * 70}")
        print(f"  Benchmark: {benchmark_path}")
        print(f"  Output: {output_dir}")
        print(f"  Harness: {args.harness or 'auto-select'}")
        print(f"  Mode: {mode}")
        print(f"  Difficulty: {args.difficulty}")
        print(f"  Duration: {duration}s ({duration / 60:.0f} min)")
        print(f"  Period: {period}s ({period / 60:.0f} min)")
        print(f"  Snapshots: {int(duration / period)}")
        print(f"  Trials: {args.trials}")
        if args.faults > 0:
            print(f"  Fault injection: {args.faults * 100:.0f}%")
        print()

        # Import here to allow script to run without full install
        try:
            from crsbench.bench_snapgen import BenchmarkSnapshotGenerator
            from crsbench.bench_snapgen.generator import load_benchmark_ground_truth
        except ImportError:
            print("Error: crsbench.bench_snapgen not found. Install with:")
            print("  uv pip install -e .")
            sys.exit(1)

        # Load benchmark data to resolve harness
        benchmark_data = load_benchmark_ground_truth(benchmark_path)

        # Resolve harness (auto-select if not specified)
        if args.harness:
            harness = args.harness
        else:
            harness = auto_select_harness(benchmark_data)

        # Generate snapshots
        try:
            for trial_num in range(1, args.trials + 1):
                if args.trials > 1:
                    trial_output_dir = output_dir / f"trial-{trial_num:03d}"
                    print(f"Trial {trial_num}/{args.trials}: {trial_output_dir}")
                else:
                    trial_output_dir = output_dir

                generator = BenchmarkSnapshotGenerator(
                    benchmark_path=benchmark_path,
                    output_dir=trial_output_dir,
                    trial_duration=duration,
                    snapshot_period=period,
                    harness=harness,
                )

                result_dir = generator.generate_trial_snapshots(
                    mode=mode,
                    difficulty_level=args.difficulty,
                    fault_injection_rate=args.faults,
                )

                print(f"✓ Trial {trial_num} completed: {result_dir}")

        except Exception as e:
            print(f"✗ Failed to generate snapshots for {benchmark_name}: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    print(f"\n{'=' * 70}")
    print("All snapshots generated successfully!")
    print(f"{'=' * 70}")
    if args.all:
        print(f"Output directory: {args.output or Path('/tmp/snapshots')}")
    else:
        print(f"Output directory: {output_dir}")


if __name__ == "__main__":
    main()
