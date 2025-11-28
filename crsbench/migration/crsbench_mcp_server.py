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

import os
import re
import shutil
import sys
import json
import time
import subprocess
import yaml
from typing import Optional, Tuple
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load environment variables from .env file
load_dotenv()

from crsbench.migration import mcp_config
from crsbench.utils.repo_manager import ensure_project_repository, get_repo_info_from_benchmark
from crsbench.utils.logger import get_logger

TARGET_BENCHMARK = ''

# Get logger instance
logger = get_logger(__name__)

# Create an MCP server
mcp = FastMCP("CRSBench Docker build and test.sh execution tools")


def _get_benchmark_dir(benchmark_name: str) -> str:
    """Get the benchmark directory path."""
    return os.path.join(mcp_config.BASE_BENCHMARKS_DIR, benchmark_name)


def _get_project_source_dir(benchmark_name: str) -> Optional[str]:
    """
    Get the project source directory path, ensuring it's at the correct commit.

    Uses the centralized repo_manager for commit-specific directory management.
    For delta mode: uses ref_commit (vulnerable version)
    For full mode: uses base_commit (vulnerable version)

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Path to project source directory or None if failed
    """
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        logger.error("Benchmark directory not found: %s", benchmark_dir)
        return None

    # Determine which commit to use based on mode
    try:
        repo_info = get_repo_info_from_benchmark(benchmark_dir)

        # Delta mode: use ref_commit (vulnerable version)
        if repo_info.get("ref_commit"):
            commit_to_use = repo_info["ref_commit"]
            logger.info("Delta mode: using ref_commit (vulnerable) %s", commit_to_use[:8])
        # Full mode: use base_commit (vulnerable version)
        else:
            commit_to_use = repo_info.get("base_commit")
            logger.info("Full mode: using base_commit (vulnerable) %s", commit_to_use[:8] if commit_to_use else "default")
    except Exception as e:
        logger.warning("Failed to determine mode, using default base_commit: %s", e)
        commit_to_use = None

    # Use repo_manager for consistent source management
    # It will use PROJECT_REPOS_DIR env var or default to .crsbench-repos
    source_path = ensure_project_repository(
        benchmark_dir=benchmark_dir,
        repos_dir=os.getenv("PROJECT_REPOS_DIR"),
        commit=commit_to_use,
        verbose=True
    )

    if not source_path:
        logger.error("Failed to get project source for benchmark %s", benchmark_name)
        return None

    logger.info("Using project source at: %s", source_path)
    return source_path


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

    # Read project.yaml for environment variables
    project_yaml_path = os.path.join(benchmark_dir, "project.yaml")
    with open(project_yaml_path) as f:
        project_config = yaml.safe_load(f)

    # Determine language mapping for FUZZING_LANGUAGE env var
    language = project_config.get('language', 'c++')
    # Map language to OSS-Fuzz FUZZING_LANGUAGE values
    language_map = {
        'jvm': 'jvm',
        'java': 'jvm',
        'c': 'c',
        'c++': 'c++',
        'go': 'go',
        'rust': 'rust',
        'python': 'python',
    }
    fuzzing_language = language_map.get(language.lower(), language)

    # Get fuzzing engine and sanitizer (use first from list)
    fuzzing_engines = project_config.get('fuzzing_engines', ['libfuzzer'])
    fuzzing_engine = fuzzing_engines[0] if fuzzing_engines else 'libfuzzer'

    sanitizers = project_config.get('sanitizers', ['address'])
    sanitizer = sanitizers[0] if sanitizers else 'address'

    # OSS-Fuzz image tag format: gcr.io/oss-fuzz/<project-name>
    # For aixcc benchmarks: gcr.io/oss-fuzz/aixcc/<benchmark-name>
    oss_fuzz_project = f"aixcc/{benchmark_name}"
    image_tag = f"gcr.io/oss-fuzz/{oss_fuzz_project}"

    os.makedirs(mcp_config.BASE_TMP_LOGS, exist_ok=True)
    target_logs = os.path.join(mcp_config.BASE_TMP_LOGS, f'test-sh-log-{benchmark_name}.txt')

    if os.path.isfile(target_logs):
        os.remove(target_logs)

    # Extract directory name from workdir path (e.g., /src/curl -> curl)
    work_dir_name = os.path.basename(workdir)

    # Get OSS-Fuzz work and out directories (needed for /work and /out mounts)
    oss_fuzz_root = mcp_config.OSS_FUZZ_DIR
    project_out = os.path.join(oss_fuzz_root, "build", "out", benchmark_name)
    project_work = os.path.join(oss_fuzz_root, "build", "work", benchmark_name)

    # Ensure directories exist
    os.makedirs(project_out, exist_ok=True)
    os.makedirs(project_work, exist_ok=True)

    # Run test.sh inside Docker container following benchmark CI pattern
    # Mount source and test.sh to temporary locations, then copy inside container
    # This ensures clean source state and follows competition environment pattern
    docker_cmd_parts = [
        "docker", "run",
        "--privileged",  # Required for some operations
        "--shm-size=2g",  # Shared memory size
        "--platform", "linux/amd64",  # Platform
        "--rm",  # Remove container after exit
        "-e", f"FUZZING_ENGINE={fuzzing_engine}",
        "-e", f"SANITIZER={sanitizer}",
        "-e", "ARCHITECTURE=x86_64",
        "-e", "HELPER=True",
        "-e", f"PROJECT_NAME={benchmark_name}",
        "-e", f"FUZZING_LANGUAGE={fuzzing_language}",
        "-v", f"{project_src_dir}:/local-source-mount",  # Mount source to temp location
        "-v", f"{test_sh_path}:/test-mnt.sh",  # Mount test.sh to temp location
        "-v", f"{project_work}:/work",  # Mount work directory
        "-v", f"{project_out}:/out",  # Mount out directory
        image_tag,
        "/bin/bash", "-c",
        f"pushd $SRC && rm -rf {work_dir_name} "
        f"&& cp -r /local-source-mount {work_dir_name} "
        f"&& cp /test-mnt.sh $SRC/test.sh "
        f"&& popd && bash $SRC/test.sh"
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
async def verify_bad_patch(benchmark_name: str, bad_patch_path: str) -> dict:
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
    benchmark_dir = _get_benchmark_dir(benchmark_name)

    if not os.path.isdir(benchmark_dir):
        return {
            "valid": False,
            "test_passed": False,
            "patch_applied": False,
            "output": f"Error: Benchmark directory not found: {benchmark_dir}"
        }

    if not os.path.exists(bad_patch_path):
        return {
            "valid": False,
            "test_passed": False,
            "patch_applied": False,
            "output": f"Error: bad_patch.diff not found at {bad_patch_path}"
        }

    # Get project source directory
    project_src_dir = _get_project_source_dir(benchmark_name)
    if not project_src_dir or not os.path.isdir(project_src_dir):
        return {
            "valid": False,
            "test_passed": False,
            "patch_applied": False,
            "output": f"Error: Project source directory not found for {benchmark_name}"
        }

    logger.info("Verifying bad_patch.diff for benchmark '%s'...", benchmark_name)

    output_lines = []

    # Step 1: Apply patch
    output_lines.append("=== Step 1: Applying bad_patch.diff ===")
    try:
        # Use git apply to apply the patch
        result = subprocess.run(
            ["git", "apply", bad_patch_path],
            cwd=project_src_dir,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            output_lines.append(f"Patch application failed with exit code {result.returncode}")
            output_lines.append(f"stdout: {result.stdout}")
            output_lines.append(f"stderr: {result.stderr}")
            return {
                "valid": False,
                "test_passed": False,
                "patch_applied": False,
                "output": "\n".join(output_lines)
            }

        output_lines.append("✅ Patch applied successfully")
        patch_applied = True

    except Exception as e:
        output_lines.append(f"Error applying patch: {str(e)}")
        return {
            "valid": False,
            "test_passed": False,
            "patch_applied": False,
            "output": "\n".join(output_lines)
        }

    # Step 2: Run test.sh
    output_lines.append("\n=== Step 2: Running test.sh with bad patch ===")
    try:
        test_result = await check_test_sh(benchmark_name)

        test_passed = test_result.get("success", False)
        test_returncode = test_result.get("returncode", -1)
        test_output = test_result.get("output", "")

        output_lines.append(f"test.sh exit code: {test_returncode}")
        output_lines.append(f"test.sh passed: {test_passed}")
        output_lines.append(f"\ntest.sh output (truncated):\n{test_output[:1000]}")

    except Exception as e:
        output_lines.append(f"Error running test.sh: {str(e)}")
        test_passed = False

    # Step 3: Restore original state
    output_lines.append("\n=== Step 3: Restoring original state ===")
    try:
        # Reverse the patch
        result = subprocess.run(
            ["git", "apply", "-R", bad_patch_path],
            cwd=project_src_dir,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            output_lines.append(f"⚠️  Failed to reverse patch (exit code {result.returncode})")
            output_lines.append(f"stderr: {result.stderr}")
            output_lines.append("Manual cleanup may be required!")
        else:
            output_lines.append("✅ Patch reversed successfully")

    except Exception as e:
        output_lines.append(f"⚠️  Error reversing patch: {str(e)}")
        output_lines.append("Manual cleanup may be required!")

    # Step 4: Evaluate result
    output_lines.append("\n=== Verification Result ===")

    # Valid bad_patch should:
    # 1. Apply successfully (patch_applied = True)
    # 2. Cause test.sh to fail (test_passed = False)
    is_valid = patch_applied and not test_passed

    if is_valid:
        output_lines.append("✅ VALID: bad_patch.diff causes test.sh to fail (as expected)")
    elif test_passed:
        output_lines.append("❌ INVALID: test.sh PASSED with bad patch applied")
        output_lines.append("   This means either:")
        output_lines.append("   1. bad_patch.diff is not strong enough")
        output_lines.append("   2. test.sh doesn't cover the mutated code")
        output_lines.append("   → Need to regenerate test.sh or bad_patch.diff")
    else:
        output_lines.append("❌ INVALID: Patch failed to apply or other error occurred")

    return {
        "valid": is_valid,
        "test_passed": test_passed,
        "patch_applied": patch_applied,
        "output": "\n".join(output_lines)
    }


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
