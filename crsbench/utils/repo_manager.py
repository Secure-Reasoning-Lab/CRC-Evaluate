"""
Repository manager for automatic cloning of project repositories.

This module handles automatic cloning and checkout of project repositories
needed for test.sh generation.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel

from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


class RepoInfo(BaseModel):
    """Repository information from benchmark configuration."""

    repo_url: str
    repo_name: Optional[str] = None
    base_commit: Optional[str] = None
    ref_commit: Optional[str] = None


# Global gitcache setting
USE_GITCACHE = False


def set_gitcache(enabled: bool):
    """Set global gitcache mode.

    Args:
        enabled: True to use gitcache for git operations, False otherwise

    Raises:
        RuntimeError: If gitcache is not installed when trying to enable it
    """
    global USE_GITCACHE

    if enabled:
        # Sanity check: verify gitcache is installed
        if not shutil.which("gitcache"):
            raise RuntimeError(
                "gitcache is not installed or not in PATH. "
                "Please install gitcache before enabling it. "
                "See: https://github.com/seeraven/gitcache"
            )
        logger.info("Gitcache enabled (gitcache found in PATH)")

    USE_GITCACHE = enabled
    logger.debug(f"Gitcache {'enabled' if enabled else 'disabled'}")


def run_git(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run git command, optionally with gitcache prefix.

    Args:
        args: Git command arguments (e.g., ['clone', 'https://...'])
        **kwargs: Additional arguments to pass to subprocess.run

    Returns:
        CompletedProcess result from subprocess.run

    Example:
        run_git(['clone', 'https://github.com/user/repo.git', '/dest'])
        run_git(['-C', '/repo', 'checkout', 'main'])
    """
    # Set check=True by default if not specified
    if "check" not in kwargs:
        kwargs["check"] = True

    if USE_GITCACHE:
        cmd = f"gitcache git {' '.join(args)}"
        return subprocess.run(cmd, shell=True, **kwargs)
    return subprocess.run(["git"] + args, **kwargs)


def get_repo_info_from_benchmark(benchmark_dir: str) -> RepoInfo:
    """
    Get repository information from benchmark configuration.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        RepoInfo with repo_url, repo_name (optional), base_commit, ref_commit

    Raises:
        FileNotFoundError: If configuration files are not found
        ValueError: If required fields are missing
    """
    benchmark_path = Path(benchmark_dir)

    # Read project.yaml for main_repo URL
    project_yaml = benchmark_path / "project.yaml"
    if not project_yaml.exists():
        raise FileNotFoundError(f"project.yaml not found in {benchmark_dir}")

    with project_yaml.open() as f:
        project_config = yaml.safe_load(f)

    repo_url = project_config.get("main_repo")
    if not repo_url:
        raise ValueError(f"main_repo not found in {project_yaml}")

    # Optional: explicit repo_name to use instead of deriving from URL
    repo_name = project_config.get("repo_name")

    # Read meta.yaml for commits
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_yaml.exists():
        raise FileNotFoundError(f"meta.yaml not found in {benchmark_dir}/.aixcc/")

    with meta_yaml.open() as f:
        meta_config = yaml.safe_load(f)

    # Get base_commit from delta_mode or full_mode
    base_commit = None
    ref_commit = None

    if "delta_mode" in meta_config:
        base_commit = meta_config["delta_mode"].get("base_commit")
        ref_commit = meta_config["delta_mode"].get("ref_commit")
    elif "full_mode" in meta_config:
        base_commit = meta_config["full_mode"].get("base_commit")

    return RepoInfo(
        repo_url=repo_url,
        repo_name=repo_name,
        base_commit=base_commit,
        ref_commit=ref_commit,
    )


def derive_repo_name_from_url(repo_url: str) -> str:
    """
    Derive repository directory name from URL.

    Args:
        repo_url: Git repository URL

    Returns:
        Suggested directory name

    Examples:
        >>> derive_repo_name_from_url("git@github.com:Team-Atlanta/cp-c-curl.git")
        'cp-c-curl'
        >>> derive_repo_name_from_url("https://github.com/curl/curl.git")
        'curl'
    """
    # Extract last part of URL
    name = repo_url.rstrip("/").split("/")[-1]

    # Remove .git extension
    if name.endswith(".git"):
        name = name[:-4]

    return name


def reset_and_clean_repo(repo_dir: str, *, verbose: bool = False) -> bool:
    """Reset repository to clean state and remove all untracked/ignored files.

    This performs:
    1. git reset --hard: Reset tracked files to HEAD
    2. git clean -xdf: Remove untracked files, directories, and ignored files

    Args:
        repo_dir: Path to git repository
        verbose: Enable verbose logging

    Returns:
        True if successful, False otherwise
    """
    if verbose:
        logger.info("🔄 Resetting repository to pristine state...")

    # First, reset tracked files
    reset_result = run_git(
        ["reset", "--hard"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if reset_result.returncode != 0:
        logger.warning(f"⚠️  Failed to reset repository: {reset_result.stderr}")
        return False

    # Then, remove all untracked and ignored files
    clean_result = run_git(
        ["clean", "-xdf"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if clean_result.returncode != 0:
        logger.warning(f"⚠️  Failed to clean repository: {clean_result.stderr}")
        return False

    if verbose:
        logger.info("✅ Repository reset and cleaned to pristine state")

    return True


def clone_repository(
    repo_url: str,
    target_dir: str,
    commit: Optional[str] = None,
    *,
    verbose: bool = False,
) -> bool:
    """
    Clone a git repository and optionally checkout a specific commit.

    Args:
        repo_url: Git repository URL
        target_dir: Directory to clone into
        commit: Optional commit hash to checkout
        verbose: Enable verbose logging

    Returns:
        True if successful, False otherwise
    """
    target_path = Path(target_dir)

    # Check if directory already exists
    if target_path.exists():
        if verbose:
            logger.info(
                f"Directory {target_dir} already exists, checking if it's a git repo..."
            )

        # Check if it's a git repository
        if (target_path / ".git").exists():
            # Reset to clean state to remove any local changes from previous runs
            try:
                reset_and_clean_repo(target_dir, verbose=verbose)
            except Exception as e:
                logger.warning(f"⚠️  Error resetting repository: {e}")

            if verbose:
                logger.info(f"✅ {target_dir} is already a git repository")
            return True
        logger.error(f"❌ {target_dir} exists but is not a git repository")
        return False

    # Create parent directory if needed
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Clone repository
    try:
        if verbose:
            logger.info(f"🔄 Cloning {repo_url} to {target_dir}...")

        result = run_git(
            ["clone", repo_url, str(target_dir)],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            check=False,
        )

        if result.returncode != 0:
            logger.error(f"❌ Git clone failed: {result.stderr}")
            return False

        if verbose:
            logger.info("✅ Successfully cloned repository")

        # Checkout specific commit if provided
        if commit:
            if verbose:
                logger.info(f"🔄 Checking out commit {commit}...")

            result = run_git(
                ["checkout", commit],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            if result.returncode != 0:
                logger.error(f"⚠️  Failed to checkout {commit}: {result.stderr}")
                logger.warning("Repository cloned but commit checkout failed")
                return True  # Still return True as clone succeeded

            if verbose:
                logger.info(f"✅ Checked out commit {commit}")

        return True

    except subprocess.TimeoutExpired:
        logger.error("❌ Git clone timed out")
        return False
    except Exception as e:
        logger.error(f"❌ Error cloning repository: {e}")
        return False


def get_commit_specific_cache_dir(
    repo_info: RepoInfo,
    target_commit: str,
    repos_dir: Optional[str] = None,
    *,
    verbose: bool = False,
) -> str:
    """Get commit-specific cache directory for repository.

    Creates a cache directory path using the pattern: {repos_dir}/{repo_name}-{short_commit}
    This enables parallel execution safety and cache efficiency.

    Args:
        repo_info: Repository info with repo_url and optional repo_name
        target_commit: Full commit hash to checkout
        repos_dir: Directory to store cloned repositories (default: PROJECT_REPOS_DIR env var or .crsbench-repos)
        verbose: Enable verbose logging

    Returns:
        Path to commit-specific cache directory
    """
    # Determine repos_dir
    repos_dir_path: Path
    if not repos_dir:
        # Try PROJECT_REPOS_DIR env var first, then default to .crsbench-repos
        crsbench_root = Path(__file__).parent.parent.parent.resolve()
        default_repos_dir = crsbench_root / ".crsbench-repos"
        repos_dir_path = Path(os.getenv("PROJECT_REPOS_DIR", str(default_repos_dir)))
    else:
        repos_dir_path = Path(repos_dir)

    # Use explicit repo_name if provided, otherwise derive from URL
    if repo_info.repo_name:
        repo_name = repo_info.repo_name
        if verbose:
            logger.info(f"Using explicit repo_name from project.yaml: {repo_name}")
    else:
        repo_name = derive_repo_name_from_url(repo_info.repo_url)
        if verbose:
            logger.info(f"Derived repo_name from URL: {repo_name}")

    # Create commit-specific directory: {repo_name}-{short_commit}
    short_commit = target_commit[:8]
    commit_specific_name = f"{repo_name}-{short_commit}"
    target_dir = repos_dir_path / commit_specific_name

    if verbose:
        logger.info(f"Using commit-specific directory: {target_dir}")

    return str(target_dir)


def clone_or_copy_cached_repo(
    repo_url: str,
    commit: str,
    target_dir: str,
    repos_dir: Optional[str] = None,
    repo_name: Optional[str] = None,
    *,
    verbose: bool = False,
) -> Optional[str]:
    """Clone repository or copy from cache if available.

    This function implements smart caching:
    - If target_dir is the cache directory and exists: reset and return
    - If target_dir is NOT the cache directory and cache exists: copy from cache
    - Otherwise: clone from remote

    Args:
        repo_url: Repository URL to clone from
        commit: Commit hash to checkout
        target_dir: Target directory for the repository
        repos_dir: Directory for cache storage (default: PROJECT_REPOS_DIR or .crsbench-repos)
        repo_name: Repository name for cache key (derived from URL if not provided)
        verbose: Enable verbose logging

    Returns:
        Path to repository directory, or None if failed
    """
    # Build repo_info object for cache directory calculation
    repo_info = RepoInfo(repo_url=repo_url, repo_name=repo_name)

    # Get cache directory path
    cache_dir = get_commit_specific_cache_dir(
        repo_info=repo_info, target_commit=commit, repos_dir=repos_dir, verbose=verbose
    )

    # Normalize paths for comparison
    target_dir_abs = str(Path(target_dir).resolve())
    cache_dir_abs = str(Path(cache_dir).resolve())

    # Check and verify cache if it exists
    cache_verified = False
    if Path(cache_dir).is_dir():
        try:
            if (Path(cache_dir) / ".git").exists():
                reset_and_clean_repo(cache_dir, verbose=verbose)

                # Verify commit
                result = run_git(
                    ["rev-parse", "HEAD"],
                    cwd=cache_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    current_commit = result.stdout.strip()
                    if current_commit.startswith(commit[:8]):
                        cache_verified = True
                        if verbose:
                            logger.info(
                                f"✅ Cache verified at correct commit: {cache_dir}"
                            )
                    else:
                        logger.warning(
                            f"⚠️  Cache at wrong commit: {current_commit[:8]} != {commit[:8]}, removing"
                        )
                        shutil.rmtree(cache_dir)
        except Exception as e:
            logger.warning(f"Failed to verify cache: {e}")
            # Try to remove corrupted cache
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass

    if target_dir_abs == cache_dir_abs:
        # Target is the cache directory itself
        if cache_verified:
            # Cache already verified and ready
            return target_dir

        # Cache doesn't exist or was removed - clone to it
        if verbose:
            logger.info("📦 Repository not found, cloning to cache...")

        success = clone_repository(
            repo_url=repo_url, target_dir=target_dir, commit=commit, verbose=verbose
        )
        return target_dir if success else None

    # Target is NOT the cache directory
    if cache_verified:
        # Cache exists and verified - copy from it
        if verbose:
            logger.info(f"📦 Copying from cache: {cache_dir} -> {target_dir}")
        try:
            shutil.copytree(
                cache_dir, target_dir, symlinks=True, ignore_dangling_symlinks=True
            )
            if verbose:
                logger.info(f"✅ Successfully copied from cache to {target_dir}")
            return target_dir
        except Exception as e:
            logger.warning(f"⚠️  Failed to copy from cache: {e}, will clone instead")
            # Fall through to clone

    # Cache doesn't exist or copy failed - clone directly to target
    if verbose:
        logger.info(f"📦 No cache available, cloning directly to {target_dir}...")

    success = clone_repository(
        repo_url=repo_url, target_dir=target_dir, commit=commit, verbose=verbose
    )

    if not success:
        return None

    # Set up cache for future uses
    if target_dir_abs != cache_dir_abs:
        if verbose:
            logger.info(f"📦 Setting up cache: {target_dir} -> {cache_dir}")
        try:
            shutil.copytree(
                target_dir, cache_dir, symlinks=True, ignore_dangling_symlinks=True
            )
            if verbose:
                logger.info(f"✅ Cache created at {cache_dir}")
        except Exception as e:
            logger.warning(f"⚠️  Failed to create cache: {e}")
            # Continue anyway - target_dir is still valid

    return target_dir


def ensure_project_repository(
    benchmark_dir: str,
    repos_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    commit: Optional[str] = None,
    *,
    verbose: bool = False,
) -> Optional[str]:
    """
    Ensure project repository exists at commit-specific directory, cloning if necessary.

    This function uses commit-specific directories for parallel execution safety
    and cache efficiency. Directory structure: {repos_dir}/{repo_name}-{short_commit}

    Args:
        benchmark_dir: Path to benchmark directory
        repos_dir: Directory to store cloned repositories (default: PROJECT_REPOS_DIR env var or .crsbench-repos)
        project_dir: Explicit project directory path (if provided, this is used directly)
        commit: Specific commit to checkout (if None, uses base_commit from meta.yaml)
        verbose: Enable verbose logging

    Returns:
        Path to project directory, or None if failed

    Workflow:
        1. If project_dir is provided and exists, return it
        2. If project_dir is provided but doesn't exist, try to clone there
        3. If project_dir is not provided, use commit-specific directory in repos_dir
    """
    # Setup logging
    verbose = True
    if verbose:
        configure_logger(level="INFO")

    # If explicit project_dir is provided
    if project_dir:
        project_path = Path(project_dir)
        if project_path.is_dir():
            if (project_path / ".git").exists():
                try:
                    reset_and_clean_repo(project_dir, verbose=verbose)
                except Exception as e:
                    logger.warning(f"⚠️  Error resetting repository: {e}")

            if verbose:
                logger.info(f"✅ Using existing project directory: {project_dir}")
            return project_dir

        # project_dir specified but doesn't exist - try to clone
        if verbose:
            logger.info(f"⚠️  Project directory {project_dir} not found")

    # Get repository info from benchmark
    try:
        repo_info = get_repo_info_from_benchmark(benchmark_dir)
    except Exception as e:
        logger.error(f"❌ Failed to get repository info: {e}")
        return None

    # Determine which commit to use (MUST be done before branching on project_dir)
    if commit:
        # Use explicit commit parameter
        target_commit = commit
        if verbose:
            logger.info(f"Using explicit commit parameter: {target_commit[:8]}")
    else:
        # Default to base_commit from meta.yaml
        target_commit = repo_info.base_commit
        if not target_commit:
            logger.error(f"❌ No base_commit found in meta.yaml for {benchmark_dir}")
            return None
        if verbose:
            logger.info(f"Using base_commit from meta.yaml: {target_commit[:8]}")

    # Determine target directory
    if project_dir:
        target_dir = project_dir
    else:
        # Use commit-specific directory for cache efficiency and parallel safety
        target_dir = get_commit_specific_cache_dir(
            repo_info=repo_info,
            target_commit=target_commit,
            repos_dir=repos_dir,
            verbose=verbose,
        )

    # Delegate to clone_or_copy_cached_repo helper
    return clone_or_copy_cached_repo(
        repo_url=repo_info.repo_url,
        commit=target_commit,
        target_dir=target_dir,
        repos_dir=repos_dir,
        repo_name=repo_info.repo_name,
        verbose=verbose,
    )


def find_or_clone_project(
    benchmark_name: str,
    benchmarks_root: str = "benchmarks",
    repos_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    *,
    verbose: bool = False,
) -> Optional[str]:
    """
    High-level function to find or clone project repository.

    Args:
        benchmark_name: Name of the benchmark
        benchmarks_root: Root directory containing benchmarks
        repos_dir: Directory to store cloned repositories
        project_dir: Explicit project directory (optional)
        verbose: Enable verbose logging

    Returns:
        Path to project directory, or None if failed
    """
    benchmark_path = Path(benchmarks_root) / benchmark_name

    if not benchmark_path.is_dir():
        logger.error(f"❌ Benchmark directory not found: {benchmark_path}")
        return None

    return ensure_project_repository(
        benchmark_dir=str(benchmark_path),
        repos_dir=repos_dir,
        project_dir=project_dir,
        verbose=verbose,
    )
