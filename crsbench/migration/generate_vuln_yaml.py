"""
CLI tool to generate vuln.yaml files for CRSBench CPVs.

This tool uses the VulnYamlGenerator to create vuln.yaml files by analyzing
crash logs, POV files, patches, and source code.
"""

import argparse
import os
import sys
from pathlib import Path
import yaml

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from crsbench.migration.vuln_yaml_generator import generate_vuln_yaml_for_cpv
from crsbench.utils.repo_manager import find_or_clone_project
from crsbench.utils.logger import (
    get_logger,
    log_section,
    log_summary,
    log_results,
    log_progress,
)

logger = get_logger(__name__)


def find_all_benchmarks(benchmarks_root: str):
    """
    Find all benchmarks in the benchmarks root directory.

    Args:
        benchmarks_root: Path to benchmarks root directory

    Returns:
        List of benchmark names
    """
    if not os.path.isdir(benchmarks_root):
        return []

    benchmarks = []
    for item in os.listdir(benchmarks_root):
        item_path = os.path.join(benchmarks_root, item)
        # Check if it's a directory and has .aixcc subdirectory
        if os.path.isdir(item_path) and os.path.isdir(os.path.join(item_path, ".aixcc")):
            benchmarks.append(item)

    return sorted(benchmarks)


def find_all_cpvs(benchmark_dir: str):
    """
    Find all harnesses and CPVs in a benchmark.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        List of tuples: [(harness_name, cpv_id), ...]
    """
    cpvs = []
    aixcc_dir = os.path.join(benchmark_dir, ".aixcc")

    if not os.path.isdir(aixcc_dir):
        return cpvs

    # List all harness directories
    for item in os.listdir(aixcc_dir):
        item_path = os.path.join(aixcc_dir, item)

        # Skip non-directories and meta.yaml
        if not os.path.isdir(item_path) or item in ["meta.yaml", "test_analysis.md"]:
            continue

        harness_name = item

        # Find all cpv_* directories in this harness
        for cpv_item in os.listdir(item_path):
            cpv_path = os.path.join(item_path, cpv_item)

            if os.path.isdir(cpv_path) and cpv_item.startswith("cpv_"):
                cpvs.append((harness_name, cpv_item))

    return sorted(cpvs)


def main():
    parser = argparse.ArgumentParser(
        description="Generate vuln.yaml for a CRSBench CPV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate vuln.yaml for ALL CPVs in ALL benchmarks
  python crsbench/migration/generate_vuln_yaml.py

  # Generate vuln.yaml for ALL CPVs in a specific benchmark
  python crsbench/migration/generate_vuln_yaml.py \\
    --benchmark atlanta-curl-delta-01

  # Generate vuln.yaml for a specific CPV
  python crsbench/migration/generate_vuln_yaml.py \\
    --benchmark atlanta-curl-delta-01 \\
    --harness curl_fuzzer_http \\
    --cpv cpv_0

  # Force overwrite ALL existing vuln.yaml files across all benchmarks
  python crsbench/migration/generate_vuln_yaml.py \\
    --force

  # With custom project directory
  python crsbench/migration/generate_vuln_yaml.py \\
    --benchmark atlanta-netty-delta-01 \\
    --project-dir /path/to/netty

Environment Variables:
  LITELLM_BASE_URL   - LiteLLM proxy URL (required)
  LITELLM_API_KEY    - LiteLLM API key (required)
  PROJECT_REPOS_DIR  - Directory containing cloned project repos
        """
    )

    parser.add_argument(
        "--benchmark",
        help="Benchmark name (e.g., atlanta-curl-delta-01). If not specified, processes all benchmarks."
    )

    parser.add_argument(
        "--harness",
        help="Harness name (e.g., curl_fuzzer_http). If not specified, processes all harnesses."
    )

    parser.add_argument(
        "--cpv",
        help="CPV identifier (e.g., cpv_0). If not specified, processes all CPVs."
    )

    parser.add_argument(
        "--benchmarks-root",
        default="benchmarks",
        help="Root directory containing benchmarks (default: benchmarks/)"
    )

    parser.add_argument(
        "--project-dir",
        help="Path to project source directory (if not specified, will auto-detect or clone)"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing vuln.yaml file"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output"
    )

    args = parser.parse_args()

    # Validate arguments
    if (args.harness and not args.cpv) or (args.cpv and not args.harness):
        logger.error("--harness and --cpv must be used together or both omitted")
        logger.info("To process a specific CPV: --harness <name> --cpv <id>")
        logger.info("To process all CPVs: omit both --harness and --cpv")
        sys.exit(1)

    # If harness/cpv specified, benchmark must also be specified
    if (args.harness or args.cpv) and not args.benchmark:
        logger.error("--benchmark is required when --harness or --cpv is specified")
        sys.exit(1)

    # Determine which benchmarks to process
    if args.benchmark:
        benchmarks_to_process = [args.benchmark]
    else:
        # Process all benchmarks
        benchmarks_to_process = find_all_benchmarks(args.benchmarks_root)
        if not benchmarks_to_process:
            logger.error(f"No benchmarks found in {args.benchmarks_root}")
            sys.exit(1)

    # Build list of (benchmark_name, harness_name, cpv_id) tuples to process
    tasks = []

    for benchmark_name in benchmarks_to_process:
        benchmark_dir = os.path.join(args.benchmarks_root, benchmark_name)

        if not os.path.isdir(benchmark_dir):
            logger.warning(f"Benchmark directory not found: {benchmark_dir}")
            continue

        # Determine which CPVs to process for this benchmark
        if args.harness and args.cpv:
            # Process single CPV (only for the specified benchmark)
            if benchmark_name == args.benchmark:
                tasks.append((benchmark_name, args.harness, args.cpv))
        else:
            # Process all CPVs in this benchmark
            cpvs = find_all_cpvs(benchmark_dir)
            for harness_name, cpv_id in cpvs:
                tasks.append((benchmark_name, harness_name, cpv_id))

    if not tasks:
        logger.error("No CPVs found to process")
        sys.exit(1)

    # Print summary of what we're going to process
    if args.harness and args.cpv:
        logger.info("Generating vuln.yaml for specific CPV")
        logger.info(f"  Benchmark: {args.benchmark}")
        logger.info(f"  Harness: {args.harness}")
        logger.info(f"  CPV: {args.cpv}")
    elif args.benchmark:
        logger.info("Generating vuln.yaml for ALL CPVs in benchmark")
        logger.info(f"  Benchmark: {args.benchmark}")
        logger.info(f"  Found {len(tasks)} CPV(s)")
    else:
        logger.info("Generating vuln.yaml for ALL CPVs in ALL benchmarks")
        logger.info(f"  Benchmarks root: {args.benchmarks_root}")
        logger.info(f"  Found {len(benchmarks_to_process)} benchmark(s)")
        logger.info(f"  Found {len(tasks)} total CPV(s)")

    log_section("Starting vuln.yaml generation")

    results = []
    success_count = 0
    failed_count = 0
    skipped_count = 0
    repos_dir = os.getenv("PROJECT_REPOS_DIR")

    # Filter out tasks where vuln.yaml already exists (unless --force)
    tasks_to_process = []
    for benchmark_name, harness_name, cpv_id in tasks:
        benchmark_dir = os.path.join(args.benchmarks_root, benchmark_name)
        vuln_yaml_path = os.path.join(
            benchmark_dir, ".aixcc", harness_name, cpv_id, "vuln.yaml"
        )
        if os.path.exists(vuln_yaml_path) and not args.force:
            skipped_count += 1
            results.append((benchmark_name, harness_name, cpv_id, {
                "success": True,
                "skipped": True,
                "message": "vuln.yaml already exists, skipped"
            }))
        else:
            tasks_to_process.append((benchmark_name, harness_name, cpv_id))

    if skipped_count > 0:
        logger.info(f"Skipping {skipped_count} CPV(s) with existing vuln.yaml")

    if not tasks_to_process:
        logger.success(f"All {len(tasks)} CPV(s) already have vuln.yaml files.")
        return

    logger.info(f"Processing {len(tasks_to_process)} CPV(s) without vuln.yaml...")

    for idx, (benchmark_name, harness_name, cpv_id) in enumerate(tasks_to_process, 1):
        log_progress(idx, len(tasks_to_process), f"Processing {benchmark_name}/{harness_name}/{cpv_id}")

        benchmark_dir = os.path.join(args.benchmarks_root, benchmark_name)

        # Determine project directory for this benchmark
        if args.project_dir:
            project_dir = args.project_dir
        else:
            project_dir = find_or_clone_project(
                benchmark_name=benchmark_name,
                benchmarks_root=args.benchmarks_root,
                repos_dir=repos_dir,
                project_dir=None,
                verbose=args.verbose
            )

            if not project_dir:
                logger.error(f"Could not find or clone project repository for {benchmark_name}")
                results.append((benchmark_name, harness_name, cpv_id, {
                    "success": False,
                    "message": "Could not find or clone project repository"
                }))
                failed_count += 1
                continue

        result = generate_vuln_yaml_for_cpv(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            harness_name=harness_name,
            cpv_id=cpv_id,
            project_dir=project_dir,
            force=args.force,
            verbose=args.verbose
        )

        results.append((benchmark_name, harness_name, cpv_id, result))

        if result["success"]:
            success_count += 1
            logger.success(f"Generated: {result['vuln_yaml_path']}")
        else:
            failed_count += 1
            logger.error(f"Failed: {result['message']}")

    # Print summary
    log_summary("vuln.yaml Generation", {
        "total": len(tasks),
        "generated": success_count,
        "skipped": skipped_count,
        "failed": failed_count,
    })

    # Log failed items
    failed_items = [
        (f"{bn}/{hn}/{ci}", r["message"])
        for bn, hn, ci, r in results
        if not r["success"]
    ]
    if failed_items:
        log_results(
            success_items=[],
            failed_items=failed_items,
            failed_title="Failed CPVs"
        )

    if success_count > 0:
        logger.info("Next steps:")
        logger.info("  1. Review the generated vuln.yaml files")
        logger.info("  2. Verify CWE classifications are accurate")
        logger.info("  3. Check that code locations are correct")
        logger.info("  4. Update descriptions if needed")

    # Exit with error if any failed
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
