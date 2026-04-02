"""Auto-discovery of fuzz targets for OSS-Fuzz projects without ground truth.

When a benchmark directory lacks .aixcc/meta.yaml but contains valid OSS-Fuzz
project files (project.yaml, Dockerfile, build.sh), this module can build the
project, discover fuzz target binaries, and generate a minimal meta.yaml for
discovery-only mode.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import BenchmarkConfig, FullMode, HarnessFile

logger = get_logger(__name__)

_REQUIRED_OSS_FUZZ_FILES = ("project.yaml", "Dockerfile", "build.sh")

# Binaries to skip when scanning build output (same as oss-fuzz's _get_fuzz_targets)
_SKIP_PREFIXES = ("afl-", "jazzer_")
_SKIP_NAMES = frozenset({"centipede", "llvm-symbolizer"})


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


def build_oss_fuzz_project(
    benchmark_path: Path,
    oss_fuzz_path: Path,
    sanitizer: str = "address",
) -> Path:
    """Build an OSS-Fuzz project using helper.py build_fuzzers.

    Args:
        benchmark_path: Path to the OSS-Fuzz project directory.
        oss_fuzz_path: Path to the oss-fuzz checkout.
        sanitizer: Sanitizer to build with (default: address).

    Returns:
        Path to the build output directory.

    Raises:
        RuntimeError: If the build fails.
        FileNotFoundError: If oss-fuzz helper.py is not found.
    """
    helper_py = oss_fuzz_path / "infra" / "helper.py"
    if not helper_py.exists():
        raise FileNotFoundError(
            f"oss-fuzz helper.py not found at {helper_py}. "
            f"Ensure oss_fuzz_path is set correctly (current: {oss_fuzz_path})"
        )

    project_name = benchmark_path.name

    # helper.py expects the project to be under oss-fuzz/projects/<name>/
    # Symlink/copy if not already there
    oss_fuzz_projects_dir = oss_fuzz_path / "projects" / project_name
    created_symlink = False
    if not oss_fuzz_projects_dir.exists():
        oss_fuzz_projects_dir.symlink_to(benchmark_path.resolve())
        created_symlink = True
        logger.info(
            f"Symlinked project: {oss_fuzz_projects_dir} -> {benchmark_path.resolve()}"
        )

    try:
        logger.info(
            f"Building OSS-Fuzz project '{project_name}' (sanitizer={sanitizer})..."
        )
        result = subprocess.run(
            [
                sys.executable,
                str(helper_py),
                "build_fuzzers",
                "--sanitizer",
                sanitizer,
                project_name,
            ],
            cwd=str(oss_fuzz_path),
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
    finally:
        # Clean up symlink if we created it
        if created_symlink and oss_fuzz_projects_dir.is_symlink():
            oss_fuzz_projects_dir.unlink()

    build_out_dir = oss_fuzz_path / "build" / "out" / project_name
    if not build_out_dir.is_dir():
        raise RuntimeError(f"Build output directory not found: {build_out_dir}")

    return build_out_dir


def auto_generate_meta_yaml(
    benchmark_path: Path,
    oss_fuzz_path: Path,
    sanitizer: str = "address",
) -> Path:
    """Auto-generate .aixcc/meta.yaml for an OSS-Fuzz project.

    Builds the project, discovers fuzz targets, resolves the HEAD commit,
    and writes a minimal meta.yaml suitable for discovery-only mode.

    Args:
        benchmark_path: Path to the OSS-Fuzz project directory.
        oss_fuzz_path: Path to the oss-fuzz checkout.
        sanitizer: Sanitizer to build with (default: address).

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
    build_out_dir = build_oss_fuzz_project(benchmark_path, oss_fuzz_path, sanitizer)

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
