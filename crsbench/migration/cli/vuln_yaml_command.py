"""
CLI tool to generate vuln.yaml files for CRSBench CPVs.

This tool uses the VulnYamlGenerator to create vuln.yaml files by analyzing
crash logs, POV files, patches, and source code.
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from crsbench.migration.vuln_yaml import (  # noqa: E402
    generate_vuln_yaml_for_cpv,
)
from crsbench.utils.logger import (  # noqa: E402
    get_logger,
    log_progress,
    log_results,
    log_section,
    log_summary,
)
from crsbench.utils.repo_manager import find_or_clone_project  # noqa: E402

logger = get_logger(__name__)


def find_all_benchmarks(benchmarks_root: str):
    """
    Find all benchmarks in the benchmarks root directory.

    Args:
        benchmarks_root: Path to benchmarks root directory

    Returns:
        List of benchmark names
    """
    root_path = Path(benchmarks_root)
    if not root_path.is_dir():
        return []

    benchmarks = []
    for item_path in root_path.iterdir():
        # Check if it's a directory and has .aixcc subdirectory
        if item_path.is_dir() and (item_path / ".aixcc").is_dir():
            benchmarks.append(item_path.name)

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
    aixcc_dir = Path(benchmark_dir) / ".aixcc"

    if not aixcc_dir.is_dir():
        return cpvs

    # List all harness directories
    for item_path in aixcc_dir.iterdir():
        # Skip non-directories and meta.yaml
        if not item_path.is_dir() or item_path.name in [
            "meta.yaml",
            "test_analysis.md",
        ]:
            continue

        harness_name = item_path.name

        # Find all cpv_* directories in this harness
        for cpv_path in item_path.iterdir():
            if cpv_path.is_dir() and cpv_path.name.startswith("cpv_"):
                cpvs.append((harness_name, cpv_path.name))

    return sorted(cpvs)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments for vuln.yaml generation.

    Args:
        parser: ArgumentParser to add arguments to
    """
    parser.add_argument(
        "--benchmark",
        help="Benchmark name (e.g., atlanta-curl-delta-01). If not specified, processes all.",
    )

    parser.add_argument(
        "--harness",
        help="Harness name (e.g., curl_fuzzer_http). If not specified, processes all.",
    )

    parser.add_argument(
        "--cpv",
        help="CPV identifier (e.g., cpv_0). If not specified, processes all CPVs.",
    )

    parser.add_argument(
        "--benchmarks-root",
        default="benchmarks",
        help="Root directory containing benchmarks (default: benchmarks/)",
    )

    parser.add_argument(
        "--project-dir",
        help="Path to project source directory (auto-detected if not specified)",
    )

    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing vuln.yaml file"
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of workers for parallel processing (default: 1, sequential)",
    )

    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")


def add_generate_vuln_yaml_subparser(subparsers) -> None:
    """Add 'generate-vuln-yaml' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    parser = subparsers.add_parser(
        "generate-vuln-yaml",
        help="Generate vuln.yaml for CRSBench CPVs using Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate for all CPVs in all benchmarks
  %(prog)s

  # Generate for specific benchmark
  %(prog)s --benchmark atlanta-curl-delta-01

  # Generate for specific CPV
  %(prog)s --benchmark atlanta-curl-delta-01 --harness curl_fuzzer_http --cpv cpv_0

  # Force overwrite existing files
  %(prog)s --force
        """,
    )
    _add_arguments(parser)
    parser.set_defaults(command="generate-vuln-yaml")


def run_generate_vuln_yaml(args: argparse.Namespace) -> int:
    """Execute the generate-vuln-yaml command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Validate arguments
    if (args.harness and not args.cpv) or (args.cpv and not args.harness):
        logger.error("--harness and --cpv must be used together or both omitted")
        return 1

    if (args.harness or args.cpv) and not args.benchmark:
        logger.error("--benchmark is required when --harness or --cpv is specified")
        return 1

    # Determine which benchmarks to process
    if args.benchmark:
        benchmarks_to_process = [args.benchmark]
    else:
        benchmarks_to_process = find_all_benchmarks(args.benchmarks_root)
        if not benchmarks_to_process:
            logger.error(f"No benchmarks found in {args.benchmarks_root}")
            return 1

    # Build list of (benchmark_name, harness_name, cpv_id) tuples to process
    tasks = []

    for benchmark_name in benchmarks_to_process:
        benchmark_dir = Path(args.benchmarks_root) / benchmark_name

        if not benchmark_dir.is_dir():
            logger.warning(f"Benchmark directory not found: {benchmark_dir}")
            continue

        if args.harness and args.cpv:
            if benchmark_name == args.benchmark:
                tasks.append((benchmark_name, args.harness, args.cpv))
        else:
            cpvs = find_all_cpvs(str(benchmark_dir))
            for harness_name, cpv_id in cpvs:
                tasks.append((benchmark_name, harness_name, cpv_id))

    if not tasks:
        logger.error("No CPVs found to process")
        return 1

    # Print summary
    if args.harness and args.cpv:
        logger.info(
            f"Generating vuln.yaml for {args.benchmark}/{args.harness}/{args.cpv}"
        )
    elif args.benchmark:
        logger.info(f"Generating vuln.yaml for {len(tasks)} CPV(s) in {args.benchmark}")
    else:
        logger.info(
            f"Generating vuln.yaml for {len(tasks)} CPV(s) in {len(benchmarks_to_process)} benchmark(s)"
        )

    log_section("Starting vuln.yaml generation")

    results = []
    success_count = 0
    failed_count = 0
    skipped_count = 0
    repos_dir = os.getenv("PROJECT_REPOS_DIR", ".crsbench-repos")

    # Filter tasks where vuln.yaml already exists
    tasks_to_process = []
    for benchmark_name, harness_name, cpv_id in tasks:
        benchmark_dir = Path(args.benchmarks_root) / benchmark_name
        vuln_yaml_path = benchmark_dir / ".aixcc" / harness_name / cpv_id / "vuln.yaml"
        if vuln_yaml_path.exists() and not args.force:
            skipped_count += 1
            results.append(
                (
                    benchmark_name,
                    harness_name,
                    cpv_id,
                    {
                        "success": True,
                        "skipped": True,
                        "message": "vuln.yaml already exists, skipped",
                    },
                )
            )
        else:
            tasks_to_process.append((benchmark_name, harness_name, cpv_id))

    if skipped_count > 0:
        logger.info(f"Skipping {skipped_count} CPV(s) with existing vuln.yaml")

    if not tasks_to_process:
        logger.success(f"All {len(tasks)} CPV(s) already have vuln.yaml files.")
        return 0

    num_workers = min(args.workers, len(tasks_to_process))
    if num_workers > 1:
        logger.info(
            f"Processing {len(tasks_to_process)} CPV(s) with {num_workers} parallel workers..."
        )
    else:
        logger.info(f"Processing {len(tasks_to_process)} CPV(s)...")

    def process_single_task(
        task_info: tuple[str, str, str],
    ) -> tuple[str, str, str, dict[str, Any]]:
        """Process a single CPV task."""
        benchmark_name, harness_name, cpv_id = task_info
        benchmark_dir = str(Path(args.benchmarks_root) / benchmark_name)

        if args.project_dir:
            project_dir = args.project_dir
        else:
            project_dir = find_or_clone_project(
                benchmark_name=benchmark_name,
                benchmarks_root=args.benchmarks_root,
                repos_dir=repos_dir,
                project_dir=None,
                verbose=args.verbose,
            )

            if not project_dir:
                return (
                    benchmark_name,
                    harness_name,
                    cpv_id,
                    {
                        "success": False,
                        "message": "Could not find or clone project repository",
                    },
                )

        result = generate_vuln_yaml_for_cpv(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            harness_name=harness_name,
            cpv_id=cpv_id,
            project_dir=project_dir,
            force=args.force,
            verbose=args.verbose,
        )

        return (benchmark_name, harness_name, cpv_id, result)

    # Process tasks (parallel or sequential)
    if num_workers > 1:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(process_single_task, task): task
                for task in tasks_to_process
            }
            completed = 0
            for future in as_completed(futures):
                completed += 1
                benchmark_name, harness_name, cpv_id, result = future.result()
                results.append((benchmark_name, harness_name, cpv_id, result))

                task_name = f"{benchmark_name}/{harness_name}/{cpv_id}"
                if result["success"]:
                    success_count += 1
                    logger.success(f"[{completed}/{len(tasks_to_process)}] {task_name}")
                else:
                    failed_count += 1
                    logger.error(
                        f"[{completed}/{len(tasks_to_process)}] {task_name}: {result['message']}"
                    )
    else:
        # Sequential processing
        for idx, task in enumerate(tasks_to_process, 1):
            log_progress(idx, len(tasks_to_process), f"{task[0]}/{task[1]}/{task[2]}")
            benchmark_name, harness_name, cpv_id, result = process_single_task(task)
            results.append((benchmark_name, harness_name, cpv_id, result))

            if result["success"]:
                success_count += 1
                logger.success(f"Generated: {result['vuln_yaml_path']}")
            else:
                failed_count += 1
                logger.error(f"Failed: {result['message']}")

    # Print summary
    log_summary(
        "vuln.yaml Generation",
        {
            "total": len(tasks),
            "generated": success_count,
            "skipped": skipped_count,
            "failed": failed_count,
        },
    )

    failed_items = [
        (f"{bn}/{hn}/{ci}", r["message"])
        for bn, hn, ci, r in results
        if not r["success"]
    ]
    if failed_items:
        log_results(
            success_items=[], failed_items=failed_items, failed_title="Failed CPVs"
        )

    return 1 if failed_count > 0 else 0


def main():
    """Standalone CLI entry point."""
    parser = argparse.ArgumentParser(description="Generate vuln.yaml for CRSBench CPVs")
    _add_arguments(parser)
    args = parser.parse_args()
    sys.exit(run_generate_vuln_yaml(args))


if __name__ == "__main__":
    main()
