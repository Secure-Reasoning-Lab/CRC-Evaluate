"""Auto-discovery of fuzz targets for OSS-Fuzz projects without ground truth.

When a benchmark directory lacks .aixcc/meta.yaml but contains valid OSS-Fuzz
project files (project.yaml, Dockerfile, build.sh), this module can build the
project, discover fuzz target binaries, and generate a minimal meta.yaml for
discovery-only mode.
"""

import subprocess
from pathlib import Path

import yaml

from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import BenchmarkConfig, FullMode, HarnessFile

logger = get_logger(__name__)

_REQUIRED_OSS_FUZZ_FILES = ("project.yaml", "Dockerfile", "build.sh")

# Binaries to skip when scanning build output (same as oss-fuzz's _get_fuzz_targets)
_SKIP_PREFIXES = ("afl-", "jazzer_")
_SKIP_NAMES = frozenset({"centipede", "llvm-symbolizer"})
_SKIP_SUFFIXES = (".jar",)


def is_oss_fuzz_project(benchmark_path: Path) -> bool:
    """Check if a directory is a valid OSS-Fuzz project.

    A valid OSS-Fuzz project contains project.yaml, Dockerfile, and build.sh.
    """
    return all((benchmark_path / name).exists() for name in _REQUIRED_OSS_FUZZ_FILES)


def discover_fuzz_targets(build_out_dir: Path) -> list[str]:
    """Discover fuzz target binaries in a build output directory.

    Scans for executable files, filtering out known non-target binaries.
    Uses the same logic as oss-fuzz's _get_fuzz_targets() in helper.py.

    Args:
        build_out_dir: Path to build output directory (e.g., build/out/<project>/)

    Returns:
        Sorted list of fuzz target binary names.
    """
    if not build_out_dir.is_dir():
        return []

    targets = []
    for entry in build_out_dir.iterdir():
        name = entry.name
        if any(name.startswith(prefix) for prefix in _SKIP_PREFIXES):
            continue
        if name in _SKIP_NAMES:
            continue
        if any(name.endswith(suffix) for suffix in _SKIP_SUFFIXES):
            continue
        if not entry.is_file():
            continue
        # Check executable bit (Python/JVM targets may only be root-executable)
        if not (entry.stat().st_mode & 0o111):
            continue

        targets.append(name)

    return sorted(targets)


def _resolve_base_commit(main_repo: str) -> str | None:
    """Resolve the current HEAD commit hash from a remote repository.

    Args:
        main_repo: Git remote URL.

    Returns:
        Commit hash string, or None if resolution fails.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", main_repo, "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: try via GitHub API if it looks like a github URL
    if "github.com" in main_repo:
        try:
            import urllib.request

            # Extract owner/repo from URL
            repo_path = main_repo.rstrip("/").rstrip(".git")
            repo_path = repo_path.split("github.com/")[-1]
            api_url = f"https://api.github.com/repos/{repo_path}/commits/HEAD"
            req = urllib.request.Request(api_url)
            req.add_header("Accept", "application/vnd.github.v3+json")
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json

                data = json.loads(resp.read())
                return data.get("sha")
        except Exception:
            pass

    return None


def _build_project_image(
    project_name: str,
    project_path: Path,
) -> str:
    """Build the Docker image for an OSS-Fuzz project.

    Equivalent to: docker build -t gcr.io/oss-fuzz/<project> <project_path>

    Returns:
        Docker image tag.
    """
    image_tag = f"gcr.io/oss-fuzz/{project_name}"

    logger.info(f"Building Docker image: {image_tag}")
    result = subprocess.run(
        ["docker", "build", "-t", image_tag, str(project_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        logger.error(f"Docker build stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(
            f"Failed to build Docker image for '{project_name}': "
            f"exit code {result.returncode}"
        )
    return image_tag


def build_oss_fuzz_project(
    benchmark_path: Path,
    oss_fuzz_path: Path,
    sanitizer: str = "address",
    *,
    cpuset_cpus: str | None = None,
) -> Path:
    """Build an OSS-Fuzz project with optional CPU pinning.

    Builds the project Docker image, then runs it to compile fuzz targets.
    Equivalent to helper.py build_fuzzers but with --cpuset-cpus support.

    Args:
        benchmark_path: Path to the OSS-Fuzz project directory.
        oss_fuzz_path: Path to the oss-fuzz checkout.
        sanitizer: Sanitizer to build with (default: address).
        cpuset_cpus: CPU cores to pin the build to (e.g., "0-3", "0,2,4").

    Returns:
        Path to the build output directory.

    Raises:
        RuntimeError: If the build fails.
    """
    project_name = benchmark_path.name

    # Read project.yaml for language
    project_yaml = benchmark_path / "project.yaml"
    with project_yaml.open() as f:
        project_config = yaml.safe_load(f)

    language = project_config.get("language", "c")

    # Build output and work directories
    build_out_dir = oss_fuzz_path / "build" / "out" / project_name
    build_work_dir = oss_fuzz_path / "build" / "work" / project_name
    build_out_dir.mkdir(parents=True, exist_ok=True)
    build_work_dir.mkdir(parents=True, exist_ok=True)

    # Build the project Docker image
    image_tag = _build_project_image(project_name, benchmark_path)

    # Run the build container to compile fuzz targets
    # Equivalent to what helper.py build_fuzzers_impl does
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--shm-size=2g",
    ]

    if cpuset_cpus:
        docker_cmd.extend(["--cpuset-cpus", cpuset_cpus])
        logger.info(f"CPU pinning build to cores: {cpuset_cpus}")

    # Environment variables (same as helper.py)
    env_vars = {
        "FUZZING_ENGINE": "libfuzzer",
        "SANITIZER": sanitizer,
        "ARCHITECTURE": "x86_64",
        "PROJECT_NAME": project_name,
        "FUZZING_LANGUAGE": language,
        "HELPER": "True",
    }
    for key, val in env_vars.items():
        docker_cmd.extend(["-e", f"{key}={val}"])

    # Volume mounts
    docker_cmd.extend(
        [
            "-v",
            f"{build_out_dir}:/out",
            "-v",
            f"{build_work_dir}:/work",
            image_tag,
        ]
    )

    logger.info(
        f"Building OSS-Fuzz project '{project_name}' "
        f"(sanitizer={sanitizer}, cpuset={cpuset_cpus or 'none'})..."
    )
    result = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        logger.error(f"Build stdout:\n{result.stdout[-2000:]}")
        logger.error(f"Build stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(
            f"Failed to build OSS-Fuzz project '{project_name}': "
            f"exit code {result.returncode}"
        )

    logger.info(f"Build succeeded for '{project_name}'")

    if not build_out_dir.is_dir():
        raise RuntimeError(f"Build output directory not found: {build_out_dir}")

    return build_out_dir


def auto_generate_meta_yaml(
    benchmark_path: Path,
    oss_fuzz_path: Path,
    sanitizer: str = "address",
    *,
    cpuset_cpus: str | None = None,
) -> Path:
    """Auto-generate .aixcc/meta.yaml for an OSS-Fuzz project.

    Builds the project, discovers fuzz targets, resolves the HEAD commit,
    and writes a minimal meta.yaml suitable for discovery-only mode.

    Args:
        benchmark_path: Path to the OSS-Fuzz project directory.
        oss_fuzz_path: Path to the oss-fuzz checkout.
        sanitizer: Sanitizer to build with (default: address).
        cpuset_cpus: CPU cores to pin the build to (e.g., "0-3").

    Returns:
        Path to the generated meta.yaml file.

    Raises:
        RuntimeError: If build fails or no fuzz targets found.
        FileNotFoundError: If project files or oss-fuzz helper missing.
    """
    if not is_oss_fuzz_project(benchmark_path):
        raise FileNotFoundError(
            f"Not a valid OSS-Fuzz project (missing project.yaml/Dockerfile/build.sh): "
            f"{benchmark_path}"
        )

    # Build the project
    build_out_dir = build_oss_fuzz_project(
        benchmark_path, oss_fuzz_path, sanitizer, cpuset_cpus=cpuset_cpus
    )

    # Discover fuzz targets
    targets = discover_fuzz_targets(build_out_dir)
    if not targets:
        raise RuntimeError(f"No fuzz targets found in build output: {build_out_dir}")

    logger.info(f"Discovered {len(targets)} fuzz targets: {targets}")

    # Resolve base commit from main_repo
    project_yaml_path = benchmark_path / "project.yaml"
    with project_yaml_path.open() as f:
        project_config = yaml.safe_load(f)

    main_repo = project_config.get("main_repo", "")
    base_commit = None
    if main_repo:
        base_commit = _resolve_base_commit(main_repo)
        if base_commit:
            logger.info(f"Resolved base_commit: {base_commit[:12]}")
        else:
            logger.warning(f"Could not resolve HEAD commit from {main_repo}")

    if not base_commit:
        # Use a placeholder — user can update later
        base_commit = "0" * 40
        logger.warning(
            f"Using placeholder base_commit ({base_commit}). "
            "Update .aixcc/meta.yaml with the actual commit hash."
        )

    # Generate meta.yaml
    harness_files = [
        HarnessFile(name=target, path=f"$PROJECT/{target}") for target in targets
    ]

    config = BenchmarkConfig(
        full_mode=FullMode(base_commit=base_commit),
        harness_files=harness_files,
    )

    meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
    config.to_yaml(meta_yaml_path)

    logger.info(f"Generated {meta_yaml_path} with {len(targets)} harnesses")
    return meta_yaml_path
