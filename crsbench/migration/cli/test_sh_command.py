#!/usr/bin/env python3
"""
CLI tool to generate test.sh files for CRSBench benchmarks.

Usage:
    # Single benchmark
    python generate_test_sh.py --benchmark <name> --project-dir <path>
    python generate_test_sh.py --benchmark apache-commons-compress-delta-01 --project-dir /path/to/commons-compress

    # Multiple benchmarks (parallel execution)
    python generate_test_sh.py --benchmarks bench1,bench2,bench3
    python generate_test_sh.py --benchmarks libxml2-delta-01,libxml2-delta-03 --parallel 2
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

from crsbench.migration.test_sh import (  # noqa: E402
    generate_test_sh_for_benchmark,
)
from crsbench.utils.logger import (  # noqa: E402
    get_logger,
    log_list,
    log_summary,
)
from crsbench.utils.repo_manager import (  # noqa: E402
    get_repo_info_from_benchmark,
)

logger = get_logger(__name__)


def get_test_sh_commit(benchmark_dir: str) -> tuple[str | None, str]:
    """
    Determine which commit to use for test.sh generation based on mode.

    For delta mode: use ref_commit (vulnerable version)
    For full mode: use base_commit (vulnerable version)

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        Tuple of (commit_hash, mode_name)
        - commit_hash: The commit to use (None means use default base_commit)
        - mode_name: "delta" or "full"
    """
    try:
        repo_info = get_repo_info_from_benchmark(benchmark_dir)

        # Delta mode: use ref_commit (vulnerable version)
        if repo_info.ref_commit:
            return repo_info.ref_commit, "delta"

        # Full mode: use base_commit (vulnerable version)
        return repo_info.base_commit, "full"

    except Exception:
        # Fallback to base_commit if unable to determine
        return None, "unknown"


def find_benchmark_dir(benchmark_name: str, benchmarks_root: str = "benchmarks") -> str:
    """Find the benchmark directory."""
    benchmark_dir = Path(benchmarks_root) / benchmark_name
    if not benchmark_dir.is_dir():
        raise ValueError(f"Benchmark directory not found: {benchmark_dir}")
    return str(benchmark_dir)


def find_benchmarks_without_test_sh(benchmarks_root: str = "benchmarks") -> List[str]:
    """
    Find all benchmarks that don't have test.sh.

    Args:
        benchmarks_root: Root directory containing benchmarks

    Returns:
        List of benchmark names without test.sh
    """
    benchmarks_without_test_sh = []
    root_path = Path(benchmarks_root)

    if not root_path.is_dir():
        return benchmarks_without_test_sh

    for benchmark_path in sorted(root_path.iterdir()):
        # Skip if not a directory
        if not benchmark_path.is_dir():
            continue

        # Skip if test.sh exists
        test_sh_path = benchmark_path / "test.sh"
        if test_sh_path.exists():
            continue

        # Check if it's a valid benchmark (has project.yaml or .aixcc/)
        project_yaml = benchmark_path / "project.yaml"
        aixcc_dir = benchmark_path / ".aixcc"

        if project_yaml.exists() or aixcc_dir.is_dir():
            benchmarks_without_test_sh.append(benchmark_path.name)

    return benchmarks_without_test_sh


def process_single_benchmark(
    benchmark_name: str,
    benchmarks_root: str,
    project_dir: str,
    output_path: Optional[str],
    litellm_base_url: str,
    litellm_api_key: str,
    repos_dir: str,
    *,
    force: bool,
    with_docker_testing: bool,
    verbose: bool,
) -> Dict[str, Any]:
    """
    Process a single benchmark.

    Returns:
        Dictionary with result including benchmark_name and success status
    """
    result = {"benchmark_name": benchmark_name, "success": False}

    try:
        # Find benchmark directory
        benchmark_dir = find_benchmark_dir(benchmark_name, benchmarks_root)

        # Check if test.sh already exists
        test_sh_path = (
            Path(output_path) if output_path else Path(benchmark_dir) / "test.sh"
        )
        if test_sh_path.exists() and not force:
            result["message"] = (
                f"test.sh already exists at {test_sh_path} (use --force to overwrite)"
            )
            return result

        # Determine which commit to use based on mode
        commit_to_use, mode = get_test_sh_commit(benchmark_dir)
        if verbose:
            if mode == "delta":
                logger.debug(
                    f"[{benchmark_name}] Delta mode: using ref_commit (vulnerable) {commit_to_use[:8] if commit_to_use else 'unknown'}"
                )
            elif mode == "full":
                logger.debug(
                    f"[{benchmark_name}] Full mode: using base_commit (vulnerable) {commit_to_use[:8] if commit_to_use else 'unknown'}"
                )
            else:
                logger.debug(f"[{benchmark_name}] Unknown mode: using base_commit")

        # Find or clone project repository
        if verbose:
            logger.debug(f"[{benchmark_name}] Finding/cloning project repository...")

        # Use explicit commit parameter when cloning
        if not project_dir:
            # Auto-clone mode: pass commit to ensure correct version
            from crsbench.utils.repo_manager import ensure_project_repository

            proj_dir = ensure_project_repository(
                benchmark_dir=benchmark_dir,
                repos_dir=repos_dir,
                commit=commit_to_use,
                verbose=verbose,
            )
        else:
            # Explicit project_dir provided: use as-is
            proj_dir = project_dir

        if not proj_dir:
            result["message"] = "Failed to find or clone project repository"
            return result

        if verbose:
            logger.debug(f"[{benchmark_name}] Using project directory: {proj_dir}")

        # Generate test.sh
        gen_result = generate_test_sh_for_benchmark(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            project_dir=proj_dir,
            output_path=output_path,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            with_docker_testing=with_docker_testing,
            verbose=verbose,
        )

        result.update(gen_result)
        return result

    except Exception as e:
        result["message"] = f"Exception: {str(e)}"
        return result


def process_multiple_benchmarks(
    benchmark_names: List[str],
    benchmarks_root: str,
    project_dir: str,
    output_path: Optional[str],
    litellm_base_url: str,
    litellm_api_key: str,
    repos_dir: str,
    *,
    force: bool,
    with_docker_testing: bool,
    verbose: bool,
    max_workers: int = 1,
) -> Dict[str, Any]:
    """
    Process multiple benchmarks in parallel.

    Args:
        benchmark_names: List of benchmark names to process
        max_workers: Maximum number of parallel workers (default: 1 for sequential)

    Returns:
        Dictionary with overall results
    """
    results = []
    successful = []
    failed = []

    logger.info(
        f"Processing {len(benchmark_names)} benchmarks with {max_workers} workers"
    )

    if max_workers == 1:
        # Sequential processing
        for benchmark_name in benchmark_names:
            logger.info(f"Processing: {benchmark_name}")
            result = process_single_benchmark(
                benchmark_name=benchmark_name,
                benchmarks_root=benchmarks_root,
                project_dir=project_dir,
                output_path=output_path,
                litellm_base_url=litellm_base_url,
                litellm_api_key=litellm_api_key,
                repos_dir=repos_dir,
                force=force,
                with_docker_testing=with_docker_testing,
                verbose=verbose,
            )
            results.append(result)

            if result["success"]:
                successful.append(benchmark_name)
                logger.success(f"[{benchmark_name}] Success: {result['test_sh_path']}")
            else:
                failed.append(benchmark_name)
                logger.error(
                    f"[{benchmark_name}] Failed: {result.get('message', 'Unknown error')}"
                )
    else:
        # Parallel processing
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_benchmark = {
                executor.submit(
                    process_single_benchmark,
                    benchmark_name=benchmark_name,
                    benchmarks_root=benchmarks_root,
                    project_dir=project_dir,
                    output_path=output_path,
                    litellm_base_url=litellm_base_url,
                    litellm_api_key=litellm_api_key,
                    repos_dir=repos_dir,
                    force=force,
                    with_docker_testing=with_docker_testing,
                    verbose=verbose,
                ): benchmark_name
                for benchmark_name in benchmark_names
            }

            for future in as_completed(future_to_benchmark):
                benchmark_name = future_to_benchmark[future]
                try:
                    result = future.result()
                    results.append(result)

                    if result["success"]:
                        successful.append(benchmark_name)
                        logger.success(
                            f"[{benchmark_name}] Success: {result['test_sh_path']}"
                        )
                    else:
                        failed.append(benchmark_name)
                        logger.error(
                            f"[{benchmark_name}] Failed: {result.get('message', 'Unknown error')}"
                        )
                except Exception as e:
                    failed.append(benchmark_name)
                    logger.error(f"[{benchmark_name}] Exception: {str(e)}")

    # Print summary
    log_summary(
        "test.sh Generation",
        {
            "total": len(benchmark_names),
            "successful": len(successful),
            "failed": len(failed),
        },
    )

    if successful:
        log_list(successful, title="Successful", symbol="✓")

    if failed:
        log_list(failed, title="Failed", symbol="✗")

    return {
        "total": len(benchmark_names),
        "successful": successful,
        "failed": failed,
        "results": results,
    }


def _add_arguments(
    parser: argparse.ArgumentParser, *, require_benchmark: bool = True
) -> None:
    """Add arguments for test.sh generation.

    Args:
        parser: ArgumentParser to add arguments to
        require_benchmark: Whether benchmark argument is required
    """
    # Mutually exclusive: single benchmark, multiple benchmarks, or all without test.sh
    benchmark_group = parser.add_mutually_exclusive_group(required=require_benchmark)
    benchmark_group.add_argument(
        "--benchmark",
        help="Single benchmark name (e.g., apache-commons-compress-delta-01)",
    )
    benchmark_group.add_argument(
        "--benchmarks",
        help="Comma-separated list of benchmark names (e.g., bench1,bench2,bench3)",
    )
    benchmark_group.add_argument(
        "--all-missing",
        action="store_true",
        help="Generate test.sh for all benchmarks that don't have one",
    )

    parser.add_argument(
        "--project-dir",
        help="Path to project source repository (auto-cloned if not provided)",
    )
    parser.add_argument(
        "--benchmarks-root",
        default="benchmarks",
        help="Root directory containing benchmarks (default: benchmarks/)",
    )
    parser.add_argument(
        "--output",
        help="Custom output path for test.sh (only for single benchmark mode)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing test.sh"
    )
    parser.add_argument(
        "--litellm-base-url",
        help="LiteLLM proxy URL (default: LITELLM_BASE_URL or LITELLM_API_BASE env var)",
    )
    parser.add_argument(
        "--litellm-api-key", help="LiteLLM API key (default: LITELLM_API_KEY env var)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    parser.add_argument(
        "--with-docker-testing",
        action="store_true",
        default=True,
        help="Enable MCP-based iterative Docker testing (default: enabled)",
    )
    parser.add_argument(
        "--no-docker-testing",
        dest="with_docker_testing",
        action="store_false",
        help="Disable Docker testing (use simple two-phase analysis only)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel workers for multiple benchmarks (default: 1)",
    )


def add_generate_test_sh_subparser(subparsers) -> None:
    """Add 'generate-test-sh' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    parser = subparsers.add_parser(
        "generate-test-sh",
        help="Generate test.sh for CRSBench benchmarks using Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate for single benchmark
  %(prog)s --benchmark apache-commons-compress-delta-01

  # Generate for multiple benchmarks
  %(prog)s --benchmarks bench1,bench2 --parallel 2

  # Generate for all benchmarks without test.sh
  %(prog)s --all-missing
        """,
    )
    _add_arguments(parser)
    parser.set_defaults(command="generate-test-sh")


def run_generate_test_sh(args: argparse.Namespace) -> int:
    """Execute the generate-test-sh command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Validate environment
    litellm_base_url = (
        args.litellm_base_url
        or os.getenv("LITELLM_BASE_URL")
        or os.getenv("LITELLM_API_BASE")
    )
    litellm_api_key = args.litellm_api_key or os.getenv("LITELLM_API_KEY")

    if not litellm_base_url:
        logger.error(
            "LITELLM_BASE_URL or LITELLM_API_BASE must be set via "
            "--litellm-base-url or env var"
        )
        return 1

    if not litellm_api_key:
        logger.error("LITELLM_API_KEY must be set via --litellm-api-key or env var")
        return 1

    repos_dir = os.getenv("PROJECT_REPOS_DIR", ".crsbench-repos")

    # Check if all-missing mode
    if args.all_missing:
        logger.info("Scanning for benchmarks without test.sh...")
        benchmark_names = find_benchmarks_without_test_sh(args.benchmarks_root)

        if not benchmark_names:
            logger.success("All benchmarks already have test.sh!")
            return 0

        logger.info(f"Found {len(benchmark_names)} benchmarks without test.sh:")
        for name in benchmark_names:
            logger.info(f"  - {name}")

        if args.output:
            logger.warning("--output is ignored in --all-missing mode")

        result = process_multiple_benchmarks(
            benchmark_names=benchmark_names,
            benchmarks_root=args.benchmarks_root,
            project_dir=args.project_dir,
            output_path=None,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            repos_dir=repos_dir,
            force=args.force,
            with_docker_testing=args.with_docker_testing,
            verbose=args.verbose,
            max_workers=args.parallel,
        )
        return 1 if result["failed"] else 0

    # Multiple benchmarks mode
    if args.benchmarks:
        benchmark_names = [name.strip() for name in args.benchmarks.split(",")]

        if args.output:
            logger.warning("--output is ignored in multiple benchmarks mode")

        result = process_multiple_benchmarks(
            benchmark_names=benchmark_names,
            benchmarks_root=args.benchmarks_root,
            project_dir=args.project_dir,
            output_path=None,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            repos_dir=repos_dir,
            force=args.force,
            with_docker_testing=args.with_docker_testing,
            verbose=args.verbose,
            max_workers=args.parallel,
        )
        return 1 if result["failed"] else 0

    # Single benchmark mode
    benchmark_name = args.benchmark

    try:
        benchmark_dir = find_benchmark_dir(benchmark_name, args.benchmarks_root)
    except ValueError as e:
        logger.error(str(e))
        return 1

    test_sh_path = Path(args.output) if args.output else Path(benchmark_dir) / "test.sh"
    if test_sh_path.exists() and not args.force:
        logger.error(f"test.sh already exists at {test_sh_path}")
        logger.info("Use --force to overwrite")
        return 1

    commit_to_use, mode = get_test_sh_commit(benchmark_dir)

    logger.info(f"Generating test.sh for {benchmark_name}")
    logger.info(f"  Benchmark: {benchmark_dir}")
    if mode == "delta":
        logger.info("  Mode: Delta (using ref_commit - vulnerable version)")
        logger.info(f"  Commit: {commit_to_use[:8] if commit_to_use else 'unknown'}")
    elif mode == "full":
        logger.info("  Mode: Full (using base_commit - vulnerable version)")
        logger.info(f"  Commit: {commit_to_use[:8] if commit_to_use else 'unknown'}")
    else:
        logger.info("  Mode: Unknown (using base_commit)")

    if args.project_dir:
        logger.info(f"  Project (specified): {args.project_dir}")
        project_dir = args.project_dir
    else:
        logger.info(f"  Project (auto-detect/clone): Looking in {repos_dir}")
        from crsbench.utils.repo_manager import ensure_project_repository

        project_dir = ensure_project_repository(
            benchmark_dir=benchmark_dir,
            repos_dir=repos_dir,
            commit=commit_to_use,
            verbose=args.verbose,
        )

    if not project_dir:
        logger.error("Failed to find or clone project repository")
        return 1

    if args.verbose or not args.project_dir:
        logger.info(f"Using project directory: {project_dir}")

    result = generate_test_sh_for_benchmark(
        benchmark_name=benchmark_name,
        benchmark_dir=benchmark_dir,
        project_dir=project_dir,
        output_path=args.output,
        litellm_base_url=litellm_base_url,
        litellm_api_key=litellm_api_key,
        with_docker_testing=args.with_docker_testing,
        verbose=args.verbose,
    )

    if result["success"]:
        logger.success("Success!")
        logger.info(f"  test.sh: {result['test_sh_path']}")
        logger.info(f"  Analysis: {result['analysis_md_path']}")
        return 0

    logger.error(f"Failed: {result['message']}")
    return 1


def main():
    """Standalone CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate test.sh for CRSBench benchmarks using Claude Agent SDK"
    )
    _add_arguments(parser)
    args = parser.parse_args()
    sys.exit(run_generate_test_sh(args))


if __name__ == "__main__":
    main()
