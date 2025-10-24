#!/usr/bin/env python3
"""
CLI tool to generate test.sh files for CRSBench benchmarks.

Usage:
    python generate_test_sh.py --benchmark <name> --project-dir <path>
    python generate_test_sh.py --benchmark apache-commons-compress-delta-01 --project-dir /path/to/commons-compress
"""

import argparse
import os
import sys
from pathlib import Path
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


def main():
    parser = argparse.ArgumentParser(
        description="Generate test.sh for CRSBench benchmarks using Claude Agent SDK"
    )
    parser.add_argument(
        "--benchmark",
        required=True,
        help="Benchmark name (e.g., apache-commons-compress-delta-01)"
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
        help="Custom output path for test.sh (default: <benchmark>/test.sh)"
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

    # Find benchmark directory
    try:
        benchmark_dir = find_benchmark_dir(args.benchmark, args.benchmarks_root)
    except ValueError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

    # Check if test.sh already exists
    test_sh_path = args.output or os.path.join(benchmark_dir, "test.sh")
    if os.path.exists(test_sh_path) and not args.force:
        print(f"❌ Error: test.sh already exists at {test_sh_path}")
        print("   Use --force to overwrite")
        sys.exit(1)

    # Find or clone project repository
    repos_dir = os.getenv("PROJECT_REPOS_DIR", "/home/acorn421/work/team-atlanta/afc-repos")

    print(f"🚀 Generating test.sh for {args.benchmark}")
    print(f"   Benchmark: {benchmark_dir}")

    if args.project_dir:
        print(f"   Project (specified): {args.project_dir}")
    else:
        print(f"   Project (auto-detect/clone): Looking in {repos_dir}")
    print()

    project_dir = find_or_clone_project(
        benchmark_name=args.benchmark,
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
        benchmark_name=args.benchmark,
        benchmark_dir=benchmark_dir,
        project_dir=project_dir,
        output_path=args.output,
        litellm_base_url=litellm_base_url,
        litellm_api_key=litellm_api_key,
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
        print(f"      python3 infra/helper.py shell {args.benchmark}")
        print(f"      # Inside container:")
        print(f"      cd /src/{args.benchmark}")
        print(f"      ./test.sh")
        sys.exit(0)
    else:
        print()
        print(f"❌ Failed: {result['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
