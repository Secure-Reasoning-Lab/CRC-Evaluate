"""Path resolver for harness files with $REPO and $PROJECT variables.

This module resolves harness file paths containing $REPO and $PROJECT variables
to actual host filesystem paths for mounting into Docker containers during CRS execution.

Variable Semantics:
    $REPO: The cloned repository directory (where source code lives)
    $PROJECT: The OSS-Fuzz compatible project directory (containing project.yaml, build.sh)

Example:
    >>> from crsbench.evaluation.path_resolver import resolve_harness_path
    >>> from pathlib import Path
    >>>
    >>> # Resolve $REPO variable
    >>> host_path = resolve_harness_path(
    ...     "$REPO/test/harness.c",
    ...     benchmark_dir=Path("benchmarks/json-c"),
    ...     repos_dir=Path("/tmp/repos")
    ... )
    >>> print(host_path)  # /tmp/repos/json-c/test/harness.c
"""

from crsbench.utils.logger import get_logger
from pathlib import Path
from typing import Optional, Tuple

from crsbench.utils.repo_manager import ensure_project_repository
from crsbench.validation.schemas import HarnessFile

logger = get_logger(__name__)


class RepositoryError(Exception):
    """Raised when repository cannot be obtained for $REPO resolution."""
    pass


def resolve_harness_path(
    harness_path: str,
    benchmark_dir: Path,
    repos_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None
) -> Path:
    """
    Resolve harness path with $REPO/$PROJECT variables to host filesystem path.

    This function translates harness paths from meta.yaml into concrete paths on the
    host filesystem that can be mounted into Docker containers.

    Args:
        harness_path: Path from meta.yaml (may contain $REPO or $PROJECT variables)
        benchmark_dir: Path to benchmark directory (e.g., benchmarks/json-c)
        repos_dir: Repository cache directory for cloning (optional, uses PROJECT_REPOS_DIR if not provided)
        project_dir: Explicit project directory override (optional, bypasses repo_manager)

    Returns:
        Resolved absolute path on host filesystem

    Raises:
        ValueError: If path format is invalid or unsupported
        FileNotFoundError: If resolved path doesn't exist on filesystem
        RepositoryError: If repository cloning/access fails for $REPO resolution

    Path Resolution Rules:
        - $REPO/path/to/file: Resolve using repo_manager to get cloned repository path
        - $PROJECT/path/to/file: Resolve relative to benchmark directory (project root)
        - /absolute/path: Return as-is (assumed to be container path, not validated)
        - ./relative/path: Resolve relative to benchmark directory

    Example:
        >>> # Resolve $REPO variable
        >>> path = resolve_harness_path(
        ...     "$REPO/test/harness.c",
        ...     benchmark_dir=Path("benchmarks/json-c"),
        ...     repos_dir=Path("/tmp/repos")
        ... )
        >>> print(path)  # /tmp/repos/json-c/test/harness.c
        >>>
        >>> # Resolve $PROJECT variable
        >>> path = resolve_harness_path(
        ...     "$PROJECT/fuzz.c",
        ...     benchmark_dir=Path("benchmarks/json-c")
        ... )
        >>> print(path)  # /abs/path/to/benchmarks/json-c/fuzz.c
    """
    harness_path = harness_path.strip()

    # $REPO variable resolution - uses repo_manager to get cloned repository
    if harness_path.startswith('$REPO/'):
        relative_path = harness_path[6:]  # Remove "$REPO/" prefix
        repo_path = _resolve_repo_path(benchmark_dir, repos_dir, project_dir)
        resolved = repo_path / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Repository: {repo_path}\n"
                f"  \n"
                f"  Possible causes:\n"
                f"    - File doesn't exist in repository\n"
                f"    - Wrong commit checked out\n"
                f"    - Path typo in meta.yaml"
            )

        logger.debug(f"Resolved $REPO path: {harness_path} → {resolved}")
        return resolved.absolute()

    # $PROJECT variable resolution - uses benchmark directory as project root
    elif harness_path.startswith('$PROJECT/'):
        relative_path = harness_path[9:]  # Remove "$PROJECT/" prefix
        project_path = project_dir if project_dir else benchmark_dir
        resolved = project_path / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Project dir: {project_path}\n"
                f"  \n"
                f"  Possible causes:\n"
                f"    - File doesn't exist in project directory\n"
                f"    - Path typo in meta.yaml"
            )

        logger.debug(f"Resolved $PROJECT path: {harness_path} → {resolved}")
        return resolved.absolute()

    # Absolute paths - assume container paths, return as-is without validation
    elif harness_path.startswith('/'):
        logger.debug(f"Absolute path (container): {harness_path}")
        return Path(harness_path)

    # Relative paths - resolve relative to benchmark directory
    elif harness_path.startswith('./'):
        relative_path = harness_path[2:]  # Remove "./" prefix
        resolved = benchmark_dir / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Benchmark dir: {benchmark_dir}"
            )

        logger.debug(f"Resolved relative path: {harness_path} → {resolved}")
        return resolved.absolute()

    # Invalid path format
    else:
        raise ValueError(
            f"Invalid harness path format: {harness_path}\n"
            f"  Expected one of:\n"
            f"    - $REPO/path/to/file (repository-relative)\n"
            f"    - $PROJECT/path/to/file (project-relative)\n"
            f"    - /absolute/path (container path)\n"
            f"    - ./relative/path (benchmark-relative)"
        )


def get_harness_source_path(
    harness: HarnessFile,
    benchmark_dir: Path,
    repos_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None
) -> Optional[Path]:
    """
    Resolve harness source path for passing to CRS commands via --harness-source.

    This function resolves the harness path to a host filesystem path that can be
    passed as an argument to oss-crs or oss-patch-crs commands. The CRS implementation
    then decides how to use this path (mount, copy, read, or ignore).

    Args:
        harness: HarnessFile object from meta.yaml configuration
        benchmark_dir: Path to benchmark directory
        repos_dir: Repository cache directory (optional)
        project_dir: Explicit project directory override (optional)

    Returns:
        Resolved absolute path on host filesystem, or None if resolution fails

    Example:
        >>> from crsbench.validation.schemas import HarnessFile
        >>> harness = HarnessFile(name="ossfuzz", path="$REPO/test/ossfuzz.c")
        >>> harness_path = get_harness_source_path(
        ...     harness,
        ...     benchmark_dir=Path("benchmarks/json-c"),
        ...     repos_dir=Path("/tmp/repos")
        ... )
        >>> print(harness_path)
        # /tmp/repos/json-c/test/ossfuzz.c
        >>>
        >>> # Use in CRS command
        >>> if harness_path:
        ...     cmd.extend(["--harness-source", str(harness_path)])
    """
    try:
        host_path = resolve_harness_path(
            harness.path,
            benchmark_dir,
            repos_dir,
            project_dir
        )
        logger.debug(f"Resolved harness source: {host_path}")
        return host_path
    except Exception as e:
        logger.warning(f"Could not resolve harness source path for {harness.name}: {e}")
        return None


def _resolve_repo_path(
    benchmark_dir: Path,
    repos_dir: Optional[Path],
    project_dir: Optional[Path]
) -> Path:
    """
    Get repository path for $REPO variable resolution.

    This internal helper function determines the repository path by either:
    1. Using explicitly provided project_dir (if given)
    2. Using repo_manager to clone/find the repository

    Args:
        benchmark_dir: Path to benchmark directory
        repos_dir: Repository cache directory for cloning (optional)
        project_dir: Explicit project directory override (optional)

    Returns:
        Path to repository root directory

    Raises:
        FileNotFoundError: If explicit project_dir doesn't exist
        RepositoryError: If repo_manager fails to obtain repository

    Priority:
        1. Explicit project_dir if provided (bypasses repo_manager)
        2. Clone/find repository using repo_manager
    """
    # Use explicit project_dir if provided (bypass repo_manager)
    if project_dir:
        if not project_dir.exists():
            raise FileNotFoundError(
                f"Explicit project directory not found: {project_dir}"
            )
        logger.debug(f"Using explicit project_dir: {project_dir}")
        return project_dir

    # Clone/find repository using repo_manager
    logger.debug(f"Resolving repository for benchmark: {benchmark_dir}")
    try:
        repo_path = ensure_project_repository(
            benchmark_dir=str(benchmark_dir),
            repos_dir=str(repos_dir) if repos_dir else None,
            verbose=False
        )
    except Exception as e:
        raise RepositoryError(
            f"Failed to obtain repository for benchmark: {benchmark_dir}\n"
            f"  Error: {e}\n"
            f"  \n"
            f"  Possible causes:\n"
            f"    - Network error during git clone\n"
            f"    - Invalid repository URL in project.yaml\n"
            f"    - Missing project.yaml or meta.yaml"
        ) from e

    if not repo_path:
        raise RepositoryError(
            f"repo_manager returned None for benchmark: {benchmark_dir}"
        )

    logger.debug(f"Repository resolved to: {repo_path}")
    return Path(repo_path)
