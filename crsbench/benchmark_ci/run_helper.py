"""Helper functions for running OSS-Fuzz commands and benchmark tests."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple, Optional

from crsbench.benchmark_ci.utils import get_oss_fuzz_root, get_benchmarks_root
from crsbench.utils.repo_manager import ensure_project_repository
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def run_cmd(
    cmd: List[str],
    cwd: Optional[str] = None,
    expect_fail: bool = False,
    exception: bool = True,
) -> Tuple[str, str]:
    """Run a command and return stdout/stderr.

    Args:
        cmd: Command and arguments
        cwd: Working directory
        expect_fail: Whether command is expected to fail
        exception: Whether to raise exception on unexpected result

    Returns:
        Tuple of (stdout, stderr)
    """
    logger.info(f"Running command (cwd: {cwd}): {' '.join(cmd)}")
    sys.stdout.flush()  # Ensure log is written before subprocess output

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,  # Prevent subprocess from detecting TTY
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout, stderr = process.communicate()

    # Clean ANSI escape sequences and control characters from output
    stdout = strip_ansi(stdout) if stdout else ""
    stderr = strip_ansi(stderr) if stderr else ""

    # Flush to ensure any leaked subprocess output is separated from subsequent logs
    sys.stdout.flush()
    sys.stderr.flush()

    if expect_fail:
        if process.returncode == 0 and exception:
            raise RuntimeError(
                f"Command '{' '.join(cmd)}' succeeded but was expected to fail\n"
                f"returncode: {process.returncode}\n"
                f"stdout: {stdout}\n"
                f"stderr: {stderr}"
            )
    else:
        if process.returncode != 0 and exception:
            raise RuntimeError(
                f"Command '{' '.join(cmd)}' failed with return code {process.returncode}\n"
                f"stdout: {stdout}\n"
                f"stderr: {stderr}"
            )

    return stdout, stderr


def run_helper(
    helper_command: List[str],
    expect_fail: bool = False,
    exception: bool = True
) -> Tuple[str, str]:
    """Run OSS-Fuzz helper.py command.

    Args:
        helper_command: Arguments to pass to helper.py
        expect_fail: Whether command is expected to fail
        exception: Whether to raise exception on unexpected result

    Returns:
        Tuple of (stdout, stderr)
    """
    oss_fuzz_root = get_oss_fuzz_root()
    helper_path = os.path.join(oss_fuzz_root, "infra", "helper.py")
    command = ["python", helper_path] + helper_command
    return run_cmd(command, expect_fail=expect_fail, exception=exception)


def build_benchmark(
    benchmark: str,
    engine: str,
    sanitizer: str,
    clean: bool = True,
    enable_check_build: bool = False
) -> None:
    """Build a benchmark using OSS-Fuzz infrastructure.

    This function:
    1. Ensures the project source code is cloned (using repo_manager)
    2. Passes the source code path to OSS-Fuzz build_fuzzers
    3. Optionally runs check_build to verify the build (if enable_check_build=True)

    Args:
        benchmark: Benchmark name (e.g., "curl-delta-02")
        engine: Fuzzing engine (e.g., "libfuzzer")
        sanitizer: Sanitizer (e.g., "address")
        clean: Whether to clean before building
        enable_check_build: Enable check_build validation (default: False)
    """
    logger.info(f"Building benchmark {benchmark} with engine={engine} sanitizer={sanitizer}")

    # Get benchmark directory
    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark

    if not benchmark_dir.exists():
        raise RuntimeError(f"[Error] Benchmark directory not found: {benchmark_dir}")

    # Ensure project source code is cloned using repo_manager
    logger.info(f"Ensuring source code is available for {benchmark}")
    repos_dir = os.getenv("PROJECT_REPOS_DIR")

    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=repos_dir,
        verbose=True
    )

    if not source_path:
        raise RuntimeError(
            f"[Error] Failed to obtain source code for {benchmark}. "
            "Check that PROJECT_REPOS_DIR is set and project.yaml has valid main_repo."
        )

    logger.info(f"Using source code at: {source_path}")

    # Build using OSS-Fuzz
    helper_command = ["build_fuzzers"]

    if clean:
        helper_command.append("--clean")

    helper_command.extend([
        "--engine", engine,
        "--sanitizer", sanitizer,
        "--architecture", "x86_64",
        benchmark,
        source_path  # Pass cloned source path
    ])

    run_helper(helper_command)

    if enable_check_build:
        check_build(benchmark, engine, sanitizer)
    else:
        logger.info(f"Skipping check_build (disabled by default for speed)")


def check_build(benchmark: str, engine: str, sanitizer: str) -> None:
    """Run check_build to verify fuzzers work correctly.

    Args:
        benchmark: Benchmark name
        engine: Fuzzing engine
        sanitizer: Sanitizer
    """
    logger.info(f"Running check_build for {benchmark}")

    helper_command = [
        "check_build",
        "--engine", engine,
        "--sanitizer", sanitizer,
        "--architecture", "x86_64",
        benchmark
    ]

    run_helper(helper_command)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", text)


def get_workdir_from_dockerfile(benchmark_dir: Path) -> str:
    """
    Parse WORKDIR from Dockerfile for the benchmark.

    This follows OSS-Fuzz's helper.py logic to extract the last WORKDIR
    directive from the Dockerfile.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        Absolute path to the working directory (e.g., '/src/commons-compress')
        Defaults to '/src' if no WORKDIR found
    """
    dockerfile_path = benchmark_dir / "Dockerfile"

    if not dockerfile_path.exists():
        logger.warning(f"Dockerfile not found at {dockerfile_path}, using default /src")
        return "/src"

    workdir_regex = re.compile(r'\s*WORKDIR\s*([^\s]+)')
    workdir = "/src"  # default

    try:
        with open(dockerfile_path) as f:
            lines = f.readlines()

        # Parse in reverse to get the last WORKDIR directive
        for line in reversed(lines):
            match = workdir_regex.match(line)
            if match:
                workdir = match.group(1)
                # Replace $SRC with /src
                workdir = workdir.replace('$SRC', '/src')

                # Make absolute path if relative
                if not os.path.isabs(workdir):
                    workdir = os.path.join('/src', workdir)

                workdir = os.path.normpath(workdir)
                break
    except Exception as e:
        logger.warning(f"Failed to parse WORKDIR from Dockerfile: {e}, using default /src")
        return "/src"

    logger.info(f"Parsed WORKDIR from Dockerfile: {workdir}")
    return workdir


def reproduce_pov(
    benchmark: str,
    harness_name: str,
    pov_path: str,
    error_token: str,
    expect_crash: bool,
    output_dir: Optional[Path] = None
) -> Tuple[str, str]:
    """Reproduce a POV to verify it triggers the vulnerability.

    Args:
        benchmark: Benchmark name
        harness_name: Harness name
        pov_path: Path to POV blob file
        error_token: Expected error message token (e.g., "AddressSanitizer: SEGV")
        expect_crash: Whether crash is expected (False if patched)
        output_dir: Directory to save crash logs

    Returns:
        Tuple of (stdout, stderr)

    Raises:
        Exception: If crash expectation doesn't match reality
    """
    logger.info(f"Reproducing POV for {benchmark}/{harness_name}, expect_crash={expect_crash}")

    helper_command = [
        "reproduce",
        benchmark,
        harness_name,
        pov_path
    ]

    # If we expect crash, command should fail
    # If patched, command should succeed
    stdout, stderr = run_helper(helper_command, expect_fail=expect_crash, exception=False)
    stdout_clean = strip_ansi(stdout)

    # Save crash log if output_dir is specified
    if output_dir:
        pov_name = Path(pov_path).stem
        pov_output_dir = output_dir / "povs"
        pov_output_dir.mkdir(parents=True, exist_ok=True)

        stdout_file = pov_output_dir / f"{harness_name}-{pov_name}.stdout"
        stderr_file = pov_output_dir / f"{harness_name}-{pov_name}.stderr"

        with open(stdout_file, 'w') as f:
            f.write(stdout_clean)
        with open(stderr_file, 'w') as f:
            f.write(stderr)

        if expect_crash and error_token in stdout_clean:
            logger.info(f"Saved crash log to: {stdout_file}")

    # Verify error token presence matches expectation
    if expect_crash:
        if error_token not in stdout_clean:
            raise Exception(
                f"[Error] Expected crash with error token '{error_token}' but not found in output\n"
                f"stdout: {stdout_clean}"
            )
        logger.info(f"✓ POV correctly triggers crash with error: {error_token}")
    else:
        if error_token in stdout_clean:
            raise Exception(
                f"[Error] Expected no crash after patch, but error token '{error_token}' found in output\n"
                f"stdout: {stdout_clean}"
            )
        logger.info(f"✓ POV correctly does not crash (patch works)")

    return stdout_clean, stderr


def run_test_sh(benchmark: str, expect_success: bool = True, output_dir: Optional[Path] = None) -> Tuple[str, str]:
    """Run test.sh for a benchmark inside Docker container.

    This runs test.sh inside the OSS-Fuzz Docker container, similar to how
    build.sh is executed, ensuring all build dependencies are available.

    Args:
        benchmark: Benchmark name
        expect_success: Whether test.sh is expected to succeed
        output_dir: Directory to save test.sh outputs

    Returns:
        Tuple of (stdout, stderr)

    Raises:
        RuntimeError: If test.sh result doesn't match expectation
    """
    import yaml

    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark
    test_sh_path = benchmark_dir / "test.sh"

    if not test_sh_path.exists():
        logger.warning(f"test.sh not found for {benchmark}, skipping")
        return "", ""

    logger.info(f"Running test.sh for {benchmark} inside Docker, expect_success={expect_success}")

    # Get OSS-Fuzz directories for the benchmark
    oss_fuzz_root = get_oss_fuzz_root()
    project_out = os.path.join(oss_fuzz_root, "build", "out", benchmark)
    project_work = os.path.join(oss_fuzz_root, "build", "work", benchmark)

    # Ensure project directories exist
    os.makedirs(project_out, exist_ok=True)
    os.makedirs(project_work, exist_ok=True)

    # Get source path using repo_manager
    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=os.getenv("PROJECT_REPOS_DIR"),
        verbose=False
    )

    if not source_path:
        raise RuntimeError(f"[Error] Failed to get source path for {benchmark}")

    # Make test.sh executable
    if not os.access(test_sh_path, os.X_OK):
        os.chmod(test_sh_path, 0o755)

    # Parse WORKDIR from Dockerfile to determine mount point
    workdir = get_workdir_from_dockerfile(benchmark_dir)

    # Read project.yaml for environment variables
    project_yaml = benchmark_dir / "project.yaml"
    with open(project_yaml) as f:
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

    # Run test.sh inside Docker container following OSS-Fuzz helper.py shell pattern
    # Mount source at WORKDIR, test.sh at /src/test.sh, /work and /out directories
    docker_command = [
        "docker", "run",
        "--privileged",  # Required for some operations
        "--shm-size=2g",  # Shared memory size
        "--platform", "linux/amd64",  # Platform
        "--rm",  # Remove container after exit
        "-e", f"FUZZING_ENGINE={fuzzing_engine}",
        "-e", f"SANITIZER={sanitizer}",
        "-e", "ARCHITECTURE=x86_64",
        "-e", "HELPER=True",
        "-e", f"PROJECT_NAME={benchmark}",
        "-e", f"FUZZING_LANGUAGE={fuzzing_language}",
        "-v", f"{source_path}:{workdir}",  # Mount source at WORKDIR
        "-v", f"{test_sh_path}:/src/test.sh",  # Mount test.sh at /src/test.sh
        "-v", f"{project_work}:/work",
        "-v", f"{project_out}:/out",
        f"gcr.io/oss-fuzz/{benchmark}",
        "bash", "/src/test.sh"  # Execute test.sh from /src
    ]

    logger.info(f"Executing: {' '.join(docker_command)}")

    # Run command and save artifacts even if it fails
    try:
        stdout, stderr = run_cmd(
            docker_command,
            expect_fail=not expect_success,
            exception=True
        )
    except RuntimeError as e:
        # Try to extract stdout/stderr from the error message if available
        # The run_cmd function includes stdout/stderr in the error message
        error_msg = str(e)
        stdout = ""
        stderr = ""

        # Parse stdout from error message
        if "stdout:" in error_msg:
            stdout_start = error_msg.find("stdout:") + len("stdout:")
            stderr_marker = error_msg.find("stderr:", stdout_start)
            if stderr_marker != -1:
                stdout = error_msg[stdout_start:stderr_marker].strip()
                stderr = error_msg[stderr_marker + len("stderr:"):].strip()
            else:
                stdout = error_msg[stdout_start:].strip()

        # Save artifacts before re-raising
        if output_dir:
            stdout_file = output_dir / "test.sh.stdout"
            stderr_file = output_dir / "test.sh.stderr"

            with open(stdout_file, 'w') as f:
                f.write(stdout)
            with open(stderr_file, 'w') as f:
                f.write(stderr)

            logger.info(f"Saved test.sh output to: {stdout_file} (test failed)")

        # Re-raise the original exception
        raise

    # Save test.sh outputs if output_dir is specified (success case)
    if output_dir:
        stdout_file = output_dir / "test.sh.stdout"
        stderr_file = output_dir / "test.sh.stderr"

        with open(stdout_file, 'w') as f:
            f.write(stdout)
        with open(stderr_file, 'w') as f:
            f.write(stderr)

        logger.info(f"Saved test.sh output to: {stdout_file}")

    if expect_success:
        logger.info(f"✓ test.sh succeeded for {benchmark}")
    else:
        logger.info(f"✓ test.sh correctly failed for {benchmark}")

    return stdout, stderr


def apply_patch(benchmark: str, patch_path: str) -> None:
    """Apply a patch file to benchmark source in PROJECT_REPOS_DIR.

    The patch is applied to the actual source repository, then the modified
    source will be mounted during rebuild.

    Args:
        benchmark: Benchmark name
        patch_path: Path to patch file (.diff)
    """
    logger.info(f"Applying patch {patch_path} to {benchmark}")

    # Get the actual source directory from PROJECT_REPOS_DIR
    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark

    src_dir = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=os.getenv("PROJECT_REPOS_DIR"),
        verbose=False
    )

    if not src_dir:
        raise RuntimeError(f"[Error] Failed to get source directory for {benchmark}")

    logger.info(f"Applying patch to source directory: {src_dir}")

    # Apply patch using git apply or patch command
    # First try git apply
    stdout, stderr = run_cmd(
        ["git", "apply", patch_path],
        cwd=src_dir,
        exception=False
    )

    # If git apply fails, try patch command
    if "error" in stderr.lower() or "fatal" in stderr.lower():
        logger.info("git apply failed, trying patch command...")
        run_cmd(
            ["patch", "-p1", "-i", patch_path],
            cwd=src_dir
        )

    logger.info(f"✓ Patch applied successfully to {src_dir}")


def revert_patch(benchmark: str, patch_path: str) -> None:
    """Revert a patch file from benchmark source in PROJECT_REPOS_DIR.

    Args:
        benchmark: Benchmark name
        patch_path: Path to patch file (.diff)
    """
    logger.info(f"Reverting patch {patch_path} from {benchmark}")

    # Get the actual source directory from PROJECT_REPOS_DIR
    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark

    src_dir = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=os.getenv("PROJECT_REPOS_DIR"),
        verbose=False
    )

    if not src_dir:
        raise RuntimeError(f"[Error] Failed to get source directory for {benchmark}")

    logger.info(f"Reverting patch from source directory: {src_dir}")

    # Revert patch using git apply -R
    stdout, stderr = run_cmd(
        ["git", "apply", "-R", patch_path],
        cwd=src_dir,
        exception=False
    )

    # If git apply fails, try patch -R
    if "error" in stderr.lower() or "fatal" in stderr.lower():
        logger.info("git apply -R failed, trying patch -R...")
        run_cmd(
            ["patch", "-R", "-p1", "-i", patch_path],
            cwd=src_dir
        )

    logger.info(f"✓ Patch reverted successfully from {src_dir}")
