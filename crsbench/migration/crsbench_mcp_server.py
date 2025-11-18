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
"""MCP server for CRSBench test.sh generation."""

import logging
import os
import re
import shutil
import sys
import json
import time
import subprocess
from typing import Optional
from mcp.server.fastmcp import FastMCP

from crsbench.migration import mcp_config

TARGET_BENCHMARK = ''

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[SERVER] %(asctime)s - %(name)s - %(module)s - %(funcName)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("crsbench-mcp-server")

# Create an MCP server
mcp = FastMCP("CRSBench Docker build and test.sh execution tools")


def _get_benchmark_dir(benchmark_name: str) -> str:
    """Get the benchmark directory path."""
    return os.path.join(mcp_config.BASE_BENCHMARKS_DIR, benchmark_name)


def _get_project_source_dir(benchmark_name: str) -> Optional[str]:
    """Get the project source directory path from benchmark's project.yaml or .aixcc/meta.yaml."""
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    # First, try project.yaml (OSS-Fuzz standard location)
    project_yaml = os.path.join(benchmark_dir, 'project.yaml')
    if os.path.exists(project_yaml):
        with open(project_yaml, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('main_repo:'):
                    repo_url = line.split('main_repo:')[1].strip().strip('"').strip("'")
                    # Extract project name from URL
                    # e.g., git@github.com:Team-Atlanta/cp-java-shiro-src.git -> cp-java-shiro-src
                    project_name = repo_url.rstrip('/').split('/')[-1].replace('.git', '')
                    source_path = os.path.join(mcp_config.BASE_PROJECTS_DIR, project_name)
                    logger.info("Found project source path from project.yaml: %s", source_path)
                    return source_path

    # Fallback to meta.yaml
    meta_yaml = os.path.join(benchmark_dir, '.aixcc', 'meta.yaml')
    if os.path.exists(meta_yaml):
        with open(meta_yaml, 'r', encoding='utf-8') as f:
            for line in f:
                if 'repository:' in line:
                    repo_url = line.split('repository:')[1].strip().strip('"').strip("'")
                    # Extract project name from URL
                    project_name = repo_url.rstrip('/').split('/')[-1].replace('.git', '')
                    source_path = os.path.join(mcp_config.BASE_PROJECTS_DIR, project_name)
                    logger.info("Found project source path from meta.yaml: %s", source_path)
                    return source_path

    logger.warning("Could not find repository info in project.yaml or meta.yaml for benchmark %s", benchmark_name)
    return None


def _get_workdir_from_dockerfile(benchmark_name: str) -> str:
    """
    Parse WORKDIR from the benchmark's Dockerfile.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        The WORKDIR path (default: /src if not found)
    """
    benchmark_dir = _get_benchmark_dir(benchmark_name)
    dockerfile_path = os.path.join(benchmark_dir, 'Dockerfile')

    if not os.path.exists(dockerfile_path):
        logger.warning("Dockerfile not found for benchmark %s, using default /src", benchmark_name)
        return '/src'

    # Regex pattern to match WORKDIR directive
    workdir_regex = re.compile(r'\s*WORKDIR\s*([^\s]+)')

    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Search in reverse to get the last WORKDIR (OSS-Fuzz convention)
        for line in reversed(lines):
            match = re.match(workdir_regex, line)
            if match:
                workdir = match.group(1)
                # Replace $SRC with /src
                workdir = workdir.replace('$SRC', '/src')

                # If relative path, join with /src
                if not os.path.isabs(workdir):
                    workdir = os.path.join('/src', workdir)

                # Normalize path
                workdir = os.path.normpath(workdir)
                logger.info("Found WORKDIR for benchmark %s: %s", benchmark_name, workdir)
                return workdir

    except Exception as e:
        logger.error("Failed to parse Dockerfile for benchmark %s: %s", benchmark_name, str(e))

    logger.warning("No WORKDIR found in Dockerfile for benchmark %s, using default /src", benchmark_name)
    return '/src'


def _prepare_benchmark_for_oss_fuzz(benchmark_name: str) -> Optional[str]:
    """
    Prepare benchmark for OSS-Fuzz by copying to oss-fuzz/projects/aixcc/.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        The OSS-Fuzz project name (aixcc/<benchmark_name>)
    """
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        logger.error("Benchmark directory not found: %s", benchmark_dir)
        return None

    # Create aixcc directory in oss-fuzz/projects if it doesn't exist
    os.makedirs(mcp_config.OSS_FUZZ_AIXCC_DIR, exist_ok=True)

    # Target directory in oss-fuzz/projects/aixcc/
    target_dir = os.path.join(mcp_config.OSS_FUZZ_AIXCC_DIR, benchmark_name)

    # Remove existing directory if it exists
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)

    # Copy benchmark to oss-fuzz/projects/aixcc/
    shutil.copytree(benchmark_dir, target_dir)
    logger.info("Copied benchmark %s to %s", benchmark_name, target_dir)

    # Return the OSS-Fuzz project name (relative path from projects/)
    return f"aixcc/{benchmark_name}"


def _detect_language(benchmark_dir: str) -> str:
    """Detect language from project.yaml."""
    project_yaml = os.path.join(benchmark_dir, 'project.yaml')
    if not os.path.exists(project_yaml):
        return 'unknown'

    with open(project_yaml, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('language:'):
                return line.split('language:')[1].strip().lower()

    return 'unknown'


def shorten_logs_if_needed(log_string: str) -> str:
    """Shortens the log string if it exceeds a certain length."""
    max_length = 5000
    if len(log_string) > max_length:
        return log_string[:1000] + '... [truncated] ' + log_string[-3700:]
    return log_string


@mcp.tool()
async def build_benchmark(benchmark_name: str) -> dict:
    """
    Builds a CRSBench benchmark using OSS-Fuzz helper.py build_fuzzers.

    Args:
        benchmark_name: Name of the benchmark to build (e.g., "curl-delta-01")

    Returns:
        Dictionary with 'success' (bool) and 'logs' (str) keys
    """
    # Prepare benchmark in oss-fuzz/projects/aixcc/
    oss_fuzz_project = _prepare_benchmark_for_oss_fuzz(benchmark_name)
    if not oss_fuzz_project:
        return {"success": False, "logs": f"Error: Failed to prepare benchmark {benchmark_name}"}

    # Get project source directory path
    project_source_path = _get_project_source_dir(benchmark_name)
    if not project_source_path:
        error_msg = f"Error: Could not find project source directory for benchmark '{benchmark_name}'"
        logger.error(error_msg)
        return {"success": False, "logs": error_msg}

    if not os.path.isdir(project_source_path):
        error_msg = f"Error: Project source directory does not exist: {project_source_path}"
        logger.error(error_msg)
        return {"success": False, "logs": error_msg}

    logger.info("Building benchmark '%s' using OSS-Fuzz helper.py with source path: %s",
                benchmark_name, project_source_path)

    # Build the command with source_path
    build_cmd = f"python3 infra/helper.py build_fuzzers {oss_fuzz_project} {project_source_path}"

    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)
    target_logs = os.path.join(mcp_config.BASE_TMP_LOGS, f'build-log-{benchmark_name}.txt')

    if os.path.isfile(target_logs):
        os.remove(target_logs)

    # Run build and capture logs
    with open(target_logs, 'w', encoding='utf-8') as log_stdout:
        try:
            subprocess.check_call(
                build_cmd,
                cwd=mcp_config.OSS_FUZZ_DIR,
                shell=True,
                stdout=log_stdout,
                stderr=subprocess.STDOUT,
                timeout=60 * 20
            )
            log_stdout.write("\n\nBuild succeeded.\n")
            success = True
            logger.info("Successfully built benchmark '%s'", benchmark_name)
        except subprocess.CalledProcessError as e:
            logger.error("Build failed for benchmark '%s': %s", benchmark_name, str(e))
            log_stdout.write(f"\n\nBuild failed with exit code {e.returncode}\n")
            success = False
        except subprocess.TimeoutExpired:
            logger.error("Build timed out for benchmark '%s'", benchmark_name)
            log_stdout.write("\n\nBuild timed out.\n")
            success = False

    # Read logs
    with open(target_logs, 'r', encoding='utf-8') as f:
        logs = f.read()

    logs_to_return = shorten_logs_if_needed(logs)
    return {"success": success, "logs": logs_to_return}


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
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        return {
            "success": False,
            "returncode": -1,
            "output": f"Error: Benchmark directory not found: {benchmark_dir}",
            "timed_out": False
        }

    test_sh_path = os.path.join(benchmark_dir, 'test.sh')
    if not os.path.exists(test_sh_path):
        return {
            "success": False,
            "returncode": -1,
            "output": f"Error: test.sh not found for benchmark: {benchmark_name}",
            "timed_out": False
        }

    logger.info("Checking test.sh for benchmark '%s'...", benchmark_name)

    # Get project source directory
    project_src_dir = _get_project_source_dir(benchmark_name)
    if not project_src_dir or not os.path.isdir(project_src_dir):
        return {
            "success": False,
            "returncode": -1,
            "output": f"Error: Project source directory not found for {benchmark_name}",
            "timed_out": False
        }

    # Get WORKDIR from Dockerfile
    workdir = _get_workdir_from_dockerfile(benchmark_name)

    # OSS-Fuzz image tag format: gcr.io/oss-fuzz/<project-name>
    # For aixcc benchmarks: gcr.io/oss-fuzz/aixcc/<benchmark-name>
    oss_fuzz_project = f"aixcc/{benchmark_name}"
    image_tag = f"gcr.io/oss-fuzz/{oss_fuzz_project}"

    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)
    target_logs = os.path.join(mcp_config.BASE_TMP_LOGS, f'test-sh-log-{benchmark_name}.txt')

    if os.path.isfile(target_logs):
        os.remove(target_logs)

    # Prepare Docker run command with project source mounted to WORKDIR
    docker_cmd_parts = [
        "docker", "run", "--rm",
        "-v", f"{test_sh_path}:/src/test.sh:ro",
        "-v", f"{project_src_dir}:{workdir}:rw",  # Mount project source to WORKDIR
        image_tag,
        "bash", "/src/test.sh"
    ]

    # Run test.sh inside the container
    returncode = 0
    timed_out = False

    with open(target_logs, 'w', encoding='utf-8') as log_stdout:
        try:
            result = subprocess.run(
                docker_cmd_parts,
                stdout=log_stdout,
                stderr=subprocess.STDOUT,
                timeout=60 * 10
            )
            returncode = result.returncode
            if returncode == 0:
                log_stdout.write("\n\ntest.sh execution succeeded.\n")
            else:
                log_stdout.write(f"\n\ntest.sh failed with exit code {returncode}\n")
        except subprocess.TimeoutExpired:
            logger.info("test.sh timed out for benchmark '%s'", benchmark_name)
            log_stdout.write("\n\ntest.sh execution timed out.\n")
            returncode = -1
            timed_out = True

    with open(target_logs, 'r', encoding='utf-8') as f:
        logs = f.read()

    logs_to_return = shorten_logs_if_needed(logs)
    logger.info("test.sh logs retrieved for benchmark '%s' (returncode: %d)", benchmark_name, returncode)

    return {
        "success": returncode == 0,
        "returncode": returncode,
        "output": logs_to_return,
        "timed_out": timed_out
    }


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
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        return f"Error: Benchmark directory not found: {benchmark_dir}"

    replay_build_path = os.path.join(benchmark_dir, 'replay-build.sh')
    if not os.path.exists(replay_build_path):
        return f"Error: replay-build.sh not found for benchmark: {benchmark_name}"

    logger.info("Testing replay-build.sh for benchmark '%s'...", benchmark_name)

    # Prepare benchmark in oss-fuzz/projects/aixcc/
    oss_fuzz_project = _prepare_benchmark_for_oss_fuzz(benchmark_name)
    if not oss_fuzz_project:
        return f"Error: Failed to prepare benchmark {benchmark_name}"

    # Get the base builder image
    image_tag = "gcr.io/oss-fuzz-base/base-builder"

    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)
    target_logs = os.path.join(mcp_config.BASE_TMP_LOGS, f'replay-build-log-{benchmark_name}.txt')

    if os.path.isfile(target_logs):
        os.remove(target_logs)

    # Get project source directory
    project_src_dir = _get_project_source_dir(benchmark_name)
    if not project_src_dir or not os.path.isdir(project_src_dir):
        return f"Error: Project source directory not found for {benchmark_name}"

    # Get WORKDIR from Dockerfile
    workdir = _get_workdir_from_dockerfile(benchmark_name)

    # Prepare Docker run command
    docker_cmd_parts = [
        "docker", "run", "--rm",
        "-v", f"{replay_build_path}:/src/replay-build.sh:ro",
        "-v", f"{project_src_dir}:{workdir}:rw",  # Mount project source to WORKDIR
        "-e", f"SRC=/src",
        "-e", f"OUT=/tmp/out",
        "-e", f"CC=clang",
        "-e", f"CXX=clang++",
        "-e", f"CFLAGS=-O1 -fno-omit-frame-pointer -g",
        "-e", f"CXXFLAGS=-O1 -fno-omit-frame-pointer -g",
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

    logs_to_return = shorten_logs_if_needed(logs)
    logger.info("replay-build.sh logs retrieved for benchmark '%s'", benchmark_name)
    return logs_to_return


@mcp.tool()
async def run_command_in_container(benchmark_name: str, command: str) -> str:
    """
    Run a command inside the Docker container for a benchmark.
    This is useful for checking tool availability (e.g., 'which sbt', 'mvn --version').

    Args:
        benchmark_name: Name of the benchmark
        command: Command to run inside the container (e.g., 'which sbt', 'ls /usr/bin')

    Returns:
        The output of the command
    """
    # Get project source directory
    project_src_dir = _get_project_source_dir(benchmark_name)
    if not project_src_dir or not os.path.isdir(project_src_dir):
        return f"Error: Project source directory not found for {benchmark_name}"

    # Get WORKDIR from Dockerfile
    workdir = _get_workdir_from_dockerfile(benchmark_name)

    # OSS-Fuzz image tag format
    oss_fuzz_project = f"aixcc/{benchmark_name}"
    image_tag = f"gcr.io/oss-fuzz/{oss_fuzz_project}"

    logger.info("Running command in container for benchmark '%s': %s", benchmark_name, command)

    # Prepare Docker run command with project source mounted to WORKDIR
    docker_cmd_parts = [
        "docker", "run", "--rm",
        "-v", f"{project_src_dir}:{workdir}:rw",  # Mount project source to WORKDIR
        image_tag,
        "bash", "-c", command
    ]

    try:
        result = subprocess.run(
            docker_cmd_parts,
            capture_output=True,
            text=True,
            timeout=60
        )

        output = result.stdout + result.stderr

        if result.returncode != 0:
            logger.info("Command failed with exit code %d for benchmark '%s'", result.returncode, benchmark_name)
            return f"Command failed with exit code {result.returncode}\n\nOutput:\n{output}"

        logger.info("Command succeeded for benchmark '%s'", benchmark_name)
        return output

    except subprocess.TimeoutExpired:
        logger.error("Command timed out for benchmark '%s'", benchmark_name)
        return "Error: Command execution timed out after 60 seconds"
    except Exception as e:
        logger.error("Failed to run command for benchmark '%s': %s", benchmark_name, str(e))
        return f"Error: Failed to run command: {str(e)}"


@mcp.tool()
async def get_benchmark_info(benchmark_name: str) -> dict:
    """
    Retrieves information about a CRSBench benchmark.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Dictionary containing benchmark metadata
    """
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        return {"error": f"Benchmark directory not found: {benchmark_dir}"}

    info = {
        "benchmark_name": benchmark_name,
        "benchmark_dir": benchmark_dir,
        "language": _detect_language(benchmark_dir),
        "has_dockerfile": os.path.exists(os.path.join(benchmark_dir, 'Dockerfile')),
        "has_build_sh": os.path.exists(os.path.join(benchmark_dir, 'build.sh')),
        "has_replay_build_sh": os.path.exists(os.path.join(benchmark_dir, 'replay-build.sh')),
        "has_test_sh": os.path.exists(os.path.join(benchmark_dir, 'test.sh')),
        "has_project_yaml": os.path.exists(os.path.join(benchmark_dir, 'project.yaml'))
    }

    # Get project source directory if available
    project_src = _get_project_source_dir(benchmark_name)
    if project_src:
        info["project_source_dir"] = project_src
        info["has_project_source"] = os.path.isdir(project_src)

    return info


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
