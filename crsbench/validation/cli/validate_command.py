"""CLI command for POV validation.

This module provides the `crsbench validate` CLI command for validating
POVs against benchmark variants.

Usage:
    crsbench validate <benchmark_path> [options]

Examples:
    # Validate all POVs in a benchmark
    crsbench validate benchmarks/aixcc/c/afc-curl-delta-01

    # Validate with specific harness
    crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --harness curl_fuzzer_ws

    # Validate from specific POV directory
    crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --pov-dir ./povs/

    # Output results to JSON file
    crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --output results.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import yaml

from crsbench.validation.verification.engine import VerificationEngine
from crsbench.validation.verification.models import VerificationResult

logger = logging.getLogger(__name__)


def add_validate_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the validate subcommand to argparse.

    Args:
        subparsers: Subparsers action from main argument parser
    """
    parser = subparsers.add_parser(
        "validate",
        help="Validate POVs against benchmark variants",
        description=(
            "Validate Proof of Vulnerability (POV) files against benchmark variants. "
            "Builds all required variant projects (base, ref, allpatched, cpvN) and "
            "runs each POV against them to determine which vulnerabilities are triggered."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate all POVs in a benchmark
  crsbench validate benchmarks/aixcc/c/afc-curl-delta-01

  # Validate with specific harness filter
  crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --harness curl_fuzzer_ws

  # Force rebuild of all variants
  crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --force-rebuild

  # Output results as JSON
  crsbench validate benchmarks/aixcc/c/afc-curl-delta-01 --output results.json --format json
        """,
    )

    # Required arguments
    parser.add_argument(
        "benchmark_path",
        type=Path,
        help="Path to the benchmark project directory",
    )

    # Optional arguments
    parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Filter to a specific harness name (required when using --pov)",
    )

    # POV input options (mutually exclusive)
    pov_group = parser.add_mutually_exclusive_group()
    pov_group.add_argument(
        "--pov",
        type=Path,
        default=None,
        help="Single POV file to validate (requires --harness)",
    )
    pov_group.add_argument(
        "--pov-dir",
        type=Path,
        default=None,
        help="Directory containing POV files (default: <benchmark>/.aixcc/povs/)",
    )
    parser.add_argument(
        "--oss-fuzz",
        type=Path,
        default=None,
        help="Path to oss-fuzz directory (default: ./oss-fuzz)",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild of all variant projects",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable deduplication of results",
    )
    parser.add_argument(
        "--dedup-strategy",
        type=str,
        default="patch-based",
        choices=["patch-based", "status-based", "none"],
        help="Deduplication strategy (default: patch-based)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout for reproduce operations in seconds (default: 120)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path for results",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="json",
        choices=["json", "yaml", "text"],
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    parser.set_defaults(func=run_validate)


def run_validate(args: argparse.Namespace) -> int:
    """Execute the validate command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Validate benchmark path
    if not args.benchmark_path.exists():
        logger.error(f"Benchmark path not found: {args.benchmark_path}")
        return 1

    # Validate single POV mode requires harness
    pov_file = getattr(args, "pov", None)
    if pov_file and not args.harness:
        logger.error(
            "--harness is required when using --pov for single file validation"
        )
        return 1

    if pov_file and not pov_file.exists():
        logger.error(f"POV file not found: {pov_file}")
        return 1

    # Determine oss-fuzz path
    oss_fuzz_path = args.oss_fuzz or Path("./oss-fuzz")
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    # Create verification engine
    dedup_strategy = "none" if args.no_dedup else args.dedup_strategy
    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=args.timeout,
        dedup_strategy=dedup_strategy,
    )

    logger.info(f"Validating benchmark: {args.benchmark_path}")

    # Handle single POV file vs directory
    if pov_file:
        # Single POV file mode
        results = run_single_pov_validation(
            engine=engine,
            benchmark_path=args.benchmark_path,
            pov_file=pov_file,
            harness=args.harness,
            force_rebuild=args.force_rebuild,
        )
    else:
        # Directory mode (original behavior)
        results = engine.verify_benchmark(
            benchmark_path=args.benchmark_path,
            pov_dir=args.pov_dir,
            harness_filter=args.harness,
            force_rebuild=args.force_rebuild,
            deduplicate=not args.no_dedup,
        )

    if not results:
        logger.warning("No verification results generated")
        return 0

    # Output results
    output_results(results, args.output, args.format)

    # Print summary
    print_summary(results)

    return 0


def run_single_pov_validation(
    engine: VerificationEngine,
    benchmark_path: Path,
    pov_file: Path,
    harness: str,
    force_rebuild: bool,
) -> List[VerificationResult]:
    """Run validation for a single POV file.

    Args:
        engine: VerificationEngine instance
        benchmark_path: Path to benchmark directory
        pov_file: Path to single POV file
        harness: Harness name to test against
        force_rebuild: Whether to force rebuild variants

    Returns:
        List containing single VerificationResult
    """
    from crsbench.validation.verification.models import VerificationRequest

    # Load adapter
    adapter = engine._load_adapter(benchmark_path)
    if not adapter:
        logger.error(f"Failed to load benchmark adapter for {benchmark_path}")
        return []

    # Build variants if needed
    if force_rebuild:
        engine._built_versions.pop(adapter.benchmark_name, None)

    versions = engine._get_or_build_versions(adapter)
    if not versions:
        logger.error(f"Failed to build variants for {adapter.benchmark_name}")
        return []

    # Read POV data
    pov_data = pov_file.read_bytes()
    logger.info(f"Validating POV: {pov_file.name} ({len(pov_data)} bytes)")
    logger.info(f"Harness: {harness}")

    # Create request and verify
    request = VerificationRequest(
        pov_data=pov_data,
        harness=harness,
        benchmark=adapter.benchmark_name,
        pov_id=pov_file.name,
    )

    result = engine.verify_pov(request, adapter, versions)
    return [result]


def output_results(
    results: List[VerificationResult],
    output_path: Optional[Path],
    format: str,
) -> None:
    """Output verification results.

    Args:
        results: List of verification results
        output_path: Optional output file path
        format: Output format (json, yaml, text)
    """
    result_dicts = [r.to_dict() for r in results]

    if format == "json":
        output = json.dumps(result_dicts, indent=2)
    elif format == "yaml":
        output = yaml.dump(result_dicts, default_flow_style=False)
    else:  # text
        lines = []
        for r in results:
            lines.append(str(r))
        output = "\n".join(lines)

    if output_path:
        output_path.write_text(output)
        logger.info(f"Results written to: {output_path}")
    else:
        print(output)


def print_summary(results: List[VerificationResult]) -> None:
    """Print a summary of verification results.

    Args:
        results: List of verification results
    """
    from collections import Counter

    status_counts = Counter(r.status.value for r in results)
    cpv_matches = set()
    for r in results:
        cpv_matches.update(r.cpv_matched)

    print("\n" + "=" * 50)
    print("VALIDATION SUMMARY")
    print("=" * 50)
    print(f"Total POVs verified: {len(results)}")
    print("\nStatus breakdown:")
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")
    if cpv_matches:
        print(f"\nCPVs triggered: {', '.join(sorted(cpv_matches))}")
    print("=" * 50)


def main() -> None:
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="CRSBench POV Validation Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    add_validate_subparser(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if hasattr(args, "func"):
        sys.exit(args.func(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
