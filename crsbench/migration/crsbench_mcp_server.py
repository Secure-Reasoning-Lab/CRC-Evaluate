# Copyright 2025 CRSBench Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################
"""MCP server for CRSBench test.sh generation.

This module provides MCP tools for Docker build and test operations,
using the shared run_helper utilities from crsbench.utils.
"""

import os
import sys
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load environment variables from .env file
load_dotenv()

from crsbench.migration import mcp_config
from crsbench.utils.run_helper import (
    # Configuration
    get_benchmark_dir,
    get_workdir_from_dockerfile,
    detect_language,
    get_project_config,
    # Source code management
    get_project_source_dir,
    prepare_benchmark_for_oss_fuzz,
    # Build functions
    build_benchmark_with_logging,
    # Test execution
    run_test_sh,
    # Patch management
    verify_bad_patch,
    # Container commands
    run_command_in_container,
    # Benchmark info
    get_benchmark_info,
)
from crsbench.utils.logger import get_logger

TARGET_BENCHMARK = ''

# Get logger instance
logger = get_logger(__name__)

# Create an MCP server
mcp = FastMCP("CRSBench Docker build and test.sh execution tools")


@mcp.tool()
async def build_benchmark(benchmark_name: str) -> dict:
    """
    Builds a CRSBench benchmark using OSS-Fuzz helper.py build_fuzzers.

    Args:
        benchmark_name: Name of the benchmark to build (e.g., "curl-delta-01")

    Returns:
        Dictionary with 'success' (bool) and 'logs' (str) keys
    """
    logger.info("Building benchmark '%s' using OSS-Fuzz helper.py", benchmark_name)

    result = build_benchmark_with_logging(
        benchmark_name=benchmark_name,
        oss_fuzz_root=mcp_config.OSS_FUZZ_DIR,
        log_dir=mcp_config.BASE_TMP_LOGS,
        timeout=60 * 20
    )

    return result.to_dict()


@mcp.tool()
async def check_test_sh(benchmark_name: str) -> dict:
    """
    Checks if test.sh runs successfully for a CRSBench benchmark.
    Uses OSS-Fuzz built image.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Dictionary with 'returncode' (int), 'output' (str), 'success' (bool), 'timed_out' (bool)
    """
    logger.info("Checking test.sh for benchmark '%s'...", benchmark_name)

    result = run_test_sh(
        benchmark=benchmark_name,
        expect_success=True,
        raise_exception=False,
        oss_fuzz_root=mcp_config.OSS_FUZZ_DIR,
        log_dir=mcp_config.BASE_TMP_LOGS,
        timeout=60 * 10,
        privileged=True,
    )

    return result.to_dict()


@mcp.tool()
async def check_replay_build_sh(benchmark_name: str) -> str:
    """
    Tests replay-build.sh (incremental build after patch) for a CRSBench benchmark.
    Assumes build.sh has already been run to create build cache.
    Uses OSS-Fuzz Docker environment.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        The logs from running replay-build.sh
    """
    import subprocess

    benchmark_dir = get_benchmark_dir(benchmark_name)

    if not benchmark_dir.is_dir():
        return f"Error: Benchmark directory not found: {benchmark_dir}"

    replay_build_path = benchmark_dir / 'replay-build.sh'
    if not replay_build_path.exists():
        return f"Error: replay-build.sh not found for benchmark: {benchmark_name}"

    logger.info("Testing replay-build.sh for benchmark '%s'...", benchmark_name)

    # Prepare benchmark in oss-fuzz/projects/aixcc/
    oss_fuzz_project = prepare_benchmark_for_oss_fuzz(benchmark_name, mcp_config.OSS_FUZZ_DIR)
    if not oss_fuzz_project:
        return f"Error: Failed to prepare benchmark {benchmark_name}"

    # Get the base builder image
    image_tag = "gcr.io/oss-fuzz-base/base-builder"

    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)
    target_logs = os.path.join(mcp_config.BASE_TMP_LOGS, f'replay-build-log-{benchmark_name}.txt')

    if os.path.isfile(target_logs):
        os.remove(target_logs)

    # Get project source directory
    project_src_dir = get_project_source_dir(benchmark_name)
    if not project_src_dir or not os.path.isdir(project_src_dir):
        return f"Error: Project source directory not found for {benchmark_name}"

    # Get WORKDIR from Dockerfile
    workdir = get_workdir_from_dockerfile(benchmark_dir)

    # Prepare Docker run command
    docker_cmd_parts = [
        "docker", "run", "--rm",
        "-v", f"{replay_build_path}:/src/replay-build.sh:ro",
        "-v", f"{project_src_dir}:{workdir}:rw",
        "-e", "SRC=/src",
        "-e", "OUT=/tmp/out",
        "-e", "CC=clang",
        "-e", "CXX=clang++",
        "-e", "CFLAGS=-O1 -fno-omit-frame-pointer -g",
        "-e", "CXXFLAGS=-O1 -fno-omit-frame-pointer -g",
        "-w", "/src",
        image_tag,
        "bash", "/src/replay-build.sh"
    ]

    # Run replay-build.sh inside the container
    with open(target_logs, 'w', encoding='utf-8') as log_stdout:
        try:
            subprocess.check_call(
                docker_cmd_parts,
                stdout=log_stdout,
                stderr=subprocess.STDOUT,
                timeout=60 * 20
            )
            log_stdout.write("\n\nreplay-build.sh execution succeeded.\n")
        except subprocess.CalledProcessError as e:
            logger.info("replay-build.sh failed for benchmark '%s': %s", benchmark_name, str(e))
            log_stdout.write(f"\n\nreplay-build.sh failed with exit code {e.returncode}\n")
        except subprocess.TimeoutExpired:
            logger.info("replay-build.sh timed out for benchmark '%s'", benchmark_name)
            log_stdout.write("\n\nreplay-build.sh execution timed out.\n")

    with open(target_logs, 'r', encoding='utf-8') as f:
        logs = f.read()

    logger.info("replay-build.sh logs retrieved for benchmark '%s'", benchmark_name)
    # Return full logs - MCP client can truncate if needed for display
    return logs


@mcp.tool()
async def mcp_run_command_in_container(benchmark_name: str, command: str) -> str:
    """
    Run a command inside the Docker container for a benchmark.
    This is useful for checking tool availability (e.g., 'which sbt', 'mvn --version').

    Args:
        benchmark_name: Name of the benchmark
        command: Command to run inside the container (e.g., 'which sbt', 'ls /usr/bin')

    Returns:
        The output of the command
    """
    return run_command_in_container(
        benchmark_name=benchmark_name,
        command=command,
        timeout=60,
        oss_fuzz_root=mcp_config.OSS_FUZZ_DIR
    )


@mcp.tool()
async def mcp_verify_bad_patch(benchmark_name: str, bad_patch_path: str) -> dict:
    """
    Verifies that bad_patch.diff breaks test.sh (test should fail after applying patch).

    Workflow:
    1. Apply bad_patch.diff to project source
    2. Run test.sh
    3. Check if test.sh fails (exit code != 0)
    4. Restore project source to original state

    Args:
        benchmark_name: Name of the benchmark
        bad_patch_path: Path to bad_patch.diff file

    Returns:
        Dictionary with:
        - 'valid': bool (True if patch causes test failure as expected)
        - 'test_passed': bool (True if test.sh passed with bad patch)
        - 'patch_applied': bool (True if patch applied successfully)
        - 'output': str (detailed logs)
    """
    result = verify_bad_patch(
        benchmark_name=benchmark_name,
        bad_patch_path=bad_patch_path,
        oss_fuzz_root=mcp_config.OSS_FUZZ_DIR
    )

    return result.to_dict()


@mcp.tool()
async def mcp_get_benchmark_info(benchmark_name: str) -> dict:
    """
    Retrieves information about a CRSBench benchmark.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Dictionary containing benchmark metadata
    """
    return get_benchmark_info(benchmark_name)


def start_mcp_server():
    """Starts the MCP server."""
    global TARGET_BENCHMARK

    if len(sys.argv) < 2:
        logger.error("Usage: python crsbench_mcp_server.py <benchmark_name>")
        sys.exit(1)

    benchmark_name = sys.argv[1]
    TARGET_BENCHMARK = benchmark_name

    # Ensure directories exist
    os.makedirs(mcp_config.BASE_DIR, exist_ok=True)
    os.makedirs(mcp_config.BASE_PROJECTS_DIR, exist_ok=True)
    os.makedirs(mcp_config.BASE_BENCHMARKS_DIR, exist_ok=True)
    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)

    logger.info('CRSBench MCP server target: %s', benchmark_name)

    try:
        logger.info("Starting MCP server.")
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("Caught KeyboardInterrupt.")
    logger.info('Server shut down.')


if __name__ == "__main__":
    start_mcp_server()
