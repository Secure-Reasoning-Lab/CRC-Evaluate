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
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

from crsbench.migration.test_sh_generator import generate_test_sh_for_benchmark
from crsbench.migration.repo_manager import find_or_clone_project


def find_benchmark_dir(benchmark_name: str, benchmarks_root: str = "benchmarks") -> str:
    """Find the benchmark directory."""
    benchmark_dir = os.path.join(benchmarks_root, benchmark_name)
    if not os.path.isdir(benchmark_dir):
        raise ValueError(f"Benchmark directory not found: {benchmark_dir}")
    return benchmark_dir


def find_benchmarks_without_test_sh(benchmarks_root: str = "benchmarks") -> List[str]:
    """
    Find all benchmarks that don't have test.sh.

    Args:
        benchmarks_root: Root directory containing benchmarks

    Returns:
        List of benchmark names without test.sh
    """
    benchmarks_without_test_sh = []

    if not os.path.isdir(benchmarks_root):
        return benchmarks_without_test_sh

    for benchmark_name in sorted(os.listdir(benchmarks_root)):
        benchmark_dir = os.path.join(benchmarks_root, benchmark_name)

        # Skip if not a directory
        if not os.path.isdir(benchmark_dir):
            continue

        # Skip if test.sh exists
        test_sh_path = os.path.join(benchmark_dir, "test.sh")
        if os.path.exists(test_sh_path):
            continue

        # Check if it's a valid benchmark (has project.yaml or .aixcc/)
        project_yaml = os.path.join(benchmark_dir, "project.yaml")
        aixcc_dir = os.path.join(benchmark_dir, ".aixcc")

        if os.path.exists(project_yaml) or os.path.isdir(aixcc_dir):
            benchmarks_without_test_sh.append(benchmark_name)

    return benchmarks_without_test_sh


def process_single_benchmark(
    benchmark_name: str,
    benchmarks_root: str,
    project_dir: str,
    output_path: str,
    force: bool,
    litellm_base_url: str,
    litellm_api_key: str,
    with_docker_testing: bool,
    verbose: bool,
    repos_dir: str
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
        test_sh_path = output_path or os.path.join(benchmark_dir, "test.sh")
        if os.path.exists(test_sh_path) and not force:
            result["message"] = f"test.sh already exists at {test_sh_path} (use --force to overwrite)"
            return result

        # Find or clone project repository
        if verbose:
            print(f"[{benchmark_name}] Finding/cloning project repository...")

        proj_dir = find_or_clone_project(
            benchmark_name=benchmark_name,
            benchmarks_root=benchmarks_root,
            repos_dir=repos_dir,
            project_dir=project_dir,
            verbose=verbose
        )

        if not proj_dir:
            result["message"] = "Failed to find or clone project repository"
            return result

        if verbose:
            print(f"[{benchmark_name}] Using project directory: {proj_dir}")

        # Generate test.sh
        gen_result = generate_test_sh_for_benchmark(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            project_dir=proj_dir,
            output_path=output_path,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            with_docker_testing=with_docker_testing,
            verbose=verbose
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
    output_path: str,
    force: bool,
    litellm_base_url: str,
    litellm_api_key: str,
    with_docker_testing: bool,
    verbose: bool,
    repos_dir: str,
    max_workers: int = 1
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

    print(f"🚀 Processing {len(benchmark_names)} benchmarks with {max_workers} workers")
    print()

    if max_workers == 1:
        # Sequential processing
        for benchmark_name in benchmark_names:
            print(f"📝 Processing: {benchmark_name}")
            result = process_single_benchmark(
                benchmark_name=benchmark_name,
                benchmarks_root=benchmarks_root,
                project_dir=project_dir,
                output_path=output_path,
                force=force,
                litellm_base_url=litellm_base_url,
                litellm_api_key=litellm_api_key,
                with_docker_testing=with_docker_testing,
                verbose=verbose,
                repos_dir=repos_dir
            )
            results.append(result)

            if result["success"]:
                successful.append(benchmark_name)
                print(f"✅ [{benchmark_name}] Success: {result['test_sh_path']}")
            else:
                failed.append(benchmark_name)
                print(f"❌ [{benchmark_name}] Failed: {result.get('message', 'Unknown error')}")
            print()
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
                    force=force,
                    litellm_base_url=litellm_base_url,
                    litellm_api_key=litellm_api_key,
                    with_docker_testing=with_docker_testing,
                    verbose=verbose,
                    repos_dir=repos_dir
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
                        print(f"✅ [{benchmark_name}] Success: {result['test_sh_path']}")
                    else:
                        failed.append(benchmark_name)
                        print(f"❌ [{benchmark_name}] Failed: {result.get('message', 'Unknown error')}")
                except Exception as e:
                    failed.append(benchmark_name)
                    print(f"❌ [{benchmark_name}] Exception: {str(e)}")

    # Print summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total: {len(benchmark_names)}")
    print(f"Successful: {len(successful)}")
    print(f"Failed: {len(failed)}")

    if successful:
        print()
        print("✅ Successful:")
        for name in successful:
            print(f"   - {name}")

    if failed:
        print()
        print("❌ Failed:")
        for name in failed:
            print(f"   - {name}")

    return {
        "total": len(benchmark_names),
        "successful": successful,
        "failed": failed,
        "results": results
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate test.sh for CRSBench benchmarks using Claude Agent SDK"
    )

    # Mutually exclusive: single benchmark, multiple benchmarks, or all without test.sh
    benchmark_group = parser.add_mutually_exclusive_group(required=True)
    benchmark_group.add_argument(
        "--benchmark",
        help="Single benchmark name (e.g., apache-commons-compress-delta-01)"
    )
    benchmark_group.add_argument(
        "--benchmarks",
        help="Comma-separated list of benchmark names (e.g., bench1,bench2,bench3)"
    )
    benchmark_group.add_argument(
        "--all-missing",
        action="store_true",
        help="Generate test.sh for all benchmarks that don't have one"
    )

    parser.add_argument(
        "--project-dir",
        help="Path to project source repository (auto-cloned if not provided)"
    )
    parser.add_argument(
        "--benchmarks-root",
        default="benchmarks",
        help="Root directory containing benchmarks (default: benchmarks/)"
    )
    parser.add_argument(
        "--output",
        help="Custom output path for test.sh (only for single benchmark mode)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing test.sh"
    )
    parser.add_argument(
        "--litellm-base-url",
        help="LiteLLM proxy URL (default: LITELLM_BASE_URL env var)"
    )
    parser.add_argument(
        "--litellm-api-key",
        help="LiteLLM API key (default: LITELLM_API_KEY env var)"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--with-docker-testing",
        action="store_true",
        default=True,
        help="Enable MCP-based iterative Docker testing (default: enabled, requires Docker and OSS-Fuzz)"
    )
    parser.add_argument(
        "--no-docker-testing",
        dest="with_docker_testing",
        action="store_false",
        help="Disable Docker testing (use simple two-phase analysis only)"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel workers for multiple benchmarks (default: 1 for sequential)"
    )

    args = parser.parse_args()

    # Validate environment
    litellm_base_url = args.litellm_base_url or os.getenv("LITELLM_BASE_URL")
    litellm_api_key = args.litellm_api_key or os.getenv("LITELLM_API_KEY")

    if not litellm_base_url:
        print("❌ Error: LITELLM_BASE_URL must be set in environment or passed via --litellm-base-url")
        sys.exit(1)

    if not litellm_api_key:
        print("❌ Error: LITELLM_API_KEY must be set in environment or passed via --litellm-api-key")
        sys.exit(1)

    repos_dir = os.getenv("PROJECT_REPOS_DIR", "/home/acorn421/work/team-atlanta/afc-repos")

    # Check if all-missing mode
    if args.all_missing:
        # Find all benchmarks without test.sh
        print("🔍 Scanning for benchmarks without test.sh...")
        benchmark_names = find_benchmarks_without_test_sh(args.benchmarks_root)

        if not benchmark_names:
            print("✅ All benchmarks already have test.sh!")
            sys.exit(0)

        print(f"📋 Found {len(benchmark_names)} benchmarks without test.sh:")
        for name in benchmark_names:
            print(f"   - {name}")
        print()

        if args.output:
            print("⚠️  Warning: --output is ignored in --all-missing mode")

        result = process_multiple_benchmarks(
            benchmark_names=benchmark_names,
            benchmarks_root=args.benchmarks_root,
            project_dir=args.project_dir,
            output_path=None,
            force=args.force,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            with_docker_testing=args.with_docker_testing,
            verbose=args.verbose,
            repos_dir=repos_dir,
            max_workers=args.parallel
        )

        # Exit with failure if any benchmark failed
        if result["failed"]:
            sys.exit(1)
        else:
            sys.exit(0)

    # Check if multiple benchmarks mode
    elif args.benchmarks:
        # Multiple benchmarks mode
        benchmark_names = [name.strip() for name in args.benchmarks.split(",")]

        if args.output:
            print("⚠️  Warning: --output is ignored in multiple benchmarks mode")

        result = process_multiple_benchmarks(
            benchmark_names=benchmark_names,
            benchmarks_root=args.benchmarks_root,
            project_dir=args.project_dir,
            output_path=None,
            force=args.force,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            with_docker_testing=args.with_docker_testing,
            verbose=args.verbose,
            repos_dir=repos_dir,
            max_workers=args.parallel
        )

        # Exit with failure if any benchmark failed
        if result["failed"]:
            sys.exit(1)
        else:
            sys.exit(0)

    else:
        # Single benchmark mode
        benchmark_name = args.benchmark

        # Find benchmark directory
        try:
            benchmark_dir = find_benchmark_dir(benchmark_name, args.benchmarks_root)
        except ValueError as e:
            print(f"❌ Error: {e}")
            sys.exit(1)

        # Check if test.sh already exists
        test_sh_path = args.output or os.path.join(benchmark_dir, "test.sh")
        if os.path.exists(test_sh_path) and not args.force:
            print(f"❌ Error: test.sh already exists at {test_sh_path}")
            print("   Use --force to overwrite")
            sys.exit(1)

        print(f"🚀 Generating test.sh for {benchmark_name}")
        print(f"   Benchmark: {benchmark_dir}")

        if args.project_dir:
            print(f"   Project (specified): {args.project_dir}")
        else:
            print(f"   Project (auto-detect/clone): Looking in {repos_dir}")
        print()

        project_dir = find_or_clone_project(
            benchmark_name=benchmark_name,
            benchmarks_root=args.benchmarks_root,
            repos_dir=repos_dir,
            project_dir=args.project_dir,
            verbose=args.verbose
        )

        if not project_dir:
            print("❌ Error: Failed to find or clone project repository")
            sys.exit(1)

        if args.verbose or not args.project_dir:
            print(f"✅ Using project directory: {project_dir}")
            print()

        result = generate_test_sh_for_benchmark(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            project_dir=project_dir,
            output_path=args.output,
            litellm_base_url=litellm_base_url,
            litellm_api_key=litellm_api_key,
            with_docker_testing=args.with_docker_testing,
            verbose=args.verbose
        )

        if result["success"]:
            print()
            print("✅ Success!")
            print(f"   test.sh: {result['test_sh_path']}")
            print(f"   Analysis: {result['analysis_md_path']}")
            print()
            print("Next steps:")
            print("   1. Review the generated test.sh")
            print("   2. Test it in OSS-Fuzz container:")
            print(f"      cd /path/to/oss-fuzz")
            print(f"      # Use OSS-Fuzz helper to enter container shell")
            print(f"      python3 infra/helper.py shell {benchmark_name}")
            print(f"      # Inside container:")
            print(f"      cd /src/{benchmark_name}")
            print(f"      ./test.sh")
            sys.exit(0)
        else:
            print()
            print(f"❌ Failed: {result['message']}")
            sys.exit(1)


if __name__ == "__main__":
    main()
