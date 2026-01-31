"""
Repository manager for automatic cloning of project repositories.

This module handles automatic cloning and checkout of project repositories
needed for test.sh generation.
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel

from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)

# Global setting for reference repository usage
USE_REFERENCE_REPOS = True

# Per-directory locks to prevent concurrent git operations on the same repo
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_lock = threading.Lock()


def _get_repo_lock(repo_path: str) -> threading.Lock:
    """Get or create a lock for a specific repository path.

    This ensures that only one thread can operate on a given repository
    at a time, preventing git lock conflicts.

    Args:
        repo_path: Path to the repository

    Returns:
        Lock for the repository
    """
    path_key = str(Path(repo_path).resolve())
    with _repo_locks_lock:
        if path_key not in _repo_locks:
            _repo_locks[path_key] = threading.Lock()
        return _repo_locks[path_key]


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


def set_reference_repos(enabled: bool) -> None:
    """Set global reference repository mode.

    When enabled, git clones use --reference --dissociate pattern
    to share objects between clones of the same repository.

    Args:
        enabled: True to use reference repositories, False for standard clones
    """
    global USE_REFERENCE_REPOS
    USE_REFERENCE_REPOS = enabled
    logger.debug(f"Reference repos {'enabled' if enabled else 'disabled'}")


def get_reference_repo_path(repo_url: str, repos_dir: str) -> Path:
    """Get path to reference (mirror) repository.

    Args:
        repo_url: Repository URL
        repos_dir: Base directory for repositories

    Returns:
        Path to mirror repository (e.g., .crsbench-repos/curl-mirror.git)
    """
    repo_name = derive_repo_name_from_url(repo_url)
    return Path(repos_dir) / f"{repo_name}-mirror.git"


def ensure_reference_repo(
    repo_url: str,
    repos_dir: str,
    *,
    verbose: bool = False,
) -> Optional[str]:
    """Ensure mirror reference repository exists and is updated.

    Creates a bare mirror repository that can be used as a reference
    for subsequent clones, sharing git objects to save storage.

    Args:
        repo_url: Repository URL to mirror
        repos_dir: Directory to store the mirror
        verbose: Enable verbose logging

    Returns:
        Path to mirror repository, or None if failed
    """
    mirror_path = get_reference_repo_path(repo_url, repos_dir)
    mirror_lock = _get_repo_lock(str(mirror_path))

    with mirror_lock:
        if mirror_path.exists():
            # Update existing mirror
            start_time = time.time()
            if verbose:
                logger.info(f"Updating reference repo: {mirror_path}")

            try:
                result = run_git(
                    ["remote", "update"],
                    cwd=str(mirror_path),
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                elapsed = time.time() - start_time
                if result.returncode != 0:
                    logger.warning(
                        f"Failed to update reference repo "
                        f"after {elapsed:.1f}s: {result.stderr}"
                    )
                elif verbose:
                    logger.info(f"Updated reference repo in {elapsed:.1f}s")
            except subprocess.TimeoutExpired:
                logger.warning("Reference repo update timed out")
        else:
            # Create new mirror
            start_time = time.time()
            logger.info(f"Creating reference repo: {mirror_path} from {repo_url}")

            try:
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                result = run_git(
                    ["clone", "--mirror", repo_url, str(mirror_path)],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
                elapsed = time.time() - start_time

                if result.returncode != 0:
                    logger.error(
                        f"Failed to create reference repo "
                        f"after {elapsed:.1f}s: {result.stderr}"
                    )
                    return None

                logger.info(f"Created reference repo in {elapsed:.1f}s: {mirror_path}")
            except subprocess.TimeoutExpired:
                logger.error("Reference repo creation timed out")
                return None

    return str(mirror_path)


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

    # Prevent git from thinking it's interactive (avoids terminal escape sequences)
    if "stdin" not in kwargs:
        kwargs["stdin"] = subprocess.DEVNULL

    if USE_GITCACHE:
        cmd = f"gitcache git {' '.join(args)}"
        return subprocess.run(cmd, shell=True, **kwargs)
    return subprocess.run(["git"] + args, **kwargs)


def get_repo_info_from_benchmark(
    benchmark_dir: str, *, mode: Optional[str] = None
) -> RepoInfo:
    """
    Get repository information from benchmark configuration.

    Args:
        benchmark_dir: Path to benchmark directory
        mode: Evaluation mode ('delta' or 'full'). If None, auto-detects from meta.yaml.

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

    # Get commits based on specified mode or auto-detect
    base_commit = None
    ref_commit = None

    if mode == "delta":
        if "delta_mode" in meta_config:
            base_commit = meta_config["delta_mode"].get("base_commit")
            ref_commit = meta_config["delta_mode"].get("ref_commit")
    elif mode == "full":
        if "full_mode" in meta_config:
            base_commit = meta_config["full_mode"].get("base_commit")
    else:
        # Auto-detect: prefer delta_mode if available
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
        logger.debug("Resetting repository to pristine state...")

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
        logger.debug("Repository reset and cleaned to pristine state")

    return True


def get_diff_from_repo_info(
    repo_info: RepoInfo,
    repo_dir: str,
    *,
    verbose: bool = False,
) -> str:
    """Get git diff between base_commit and ref_commit from RepoInfo.

    Args:
        repo_info: Repository info containing base_commit and ref_commit
        repo_dir: Path to git repository
        verbose: Enable verbose logging

    Returns:
        Diff content as string

    Raises:
        ValueError: If base_commit or ref_commit is missing from repo_info
        RuntimeError: If git diff command fails
    """
    # Validate commits exist
    if not repo_info.base_commit:
        raise ValueError("base_commit is required in repo_info")
    if not repo_info.ref_commit:
        raise ValueError("ref_commit is required in repo_info")

    if verbose:
        logger.info(
            f"🔄 Generating diff between {repo_info.base_commit[:8]} and {repo_info.ref_commit[:8]}..."
        )

    # Run git diff
    result = run_git(
        ["diff", repo_info.base_commit, repo_info.ref_commit],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Git diff failed with exit code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    if verbose:
        logger.info("✅ Successfully generated diff")

    return result.stdout


def write_diff_from_repo_info(
    repo_info: RepoInfo,
    repo_dir: str,
    output_path: str,
    *,
    verbose: bool = False,
) -> str:
    """Write git diff to file from RepoInfo.

    Args:
        repo_info: Repository info containing base_commit and ref_commit
        repo_dir: Path to git repository
        output_path: Path to write diff file
        verbose: Enable verbose logging

    Returns:
        Path to the written diff file

    Raises:
        ValueError: If base_commit or ref_commit is missing from repo_info
        RuntimeError: If git diff command fails
    """
    # Get diff content
    diff_content = get_diff_from_repo_info(repo_info, repo_dir, verbose=verbose)

    # Create parent directories if needed
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Write diff to file
    output_file.write_text(diff_content, encoding="utf-8")

    if verbose:
        logger.info(f"✅ Diff written to {output_path}")

    return output_path


def write_benchmark_delta_diff(
    benchmark_dir: str,
    output_path: str,
    repos_dir: Optional[str] = None,
    *,
    verbose: bool = False,
) -> str:
    """Write git diff from benchmark delta mode.

    Args:
        benchmark_dir: Path to benchmark directory
        output_path: Path to write diff file
        repos_dir: Directory for repository cache (optional)
        verbose: Enable verbose logging

    Returns:
        Path to the written diff file

    Raises:
        ValueError: If benchmark doesn't have delta mode (no ref_commit or base_commit)
        FileNotFoundError: If benchmark configuration files are not found
        RuntimeError: If git operations fail
    """
    # Get repository info from benchmark (delta mode)
    repo_info = get_repo_info_from_benchmark(benchmark_dir, mode="delta")

    # Validate delta mode exists
    if not repo_info.ref_commit:
        raise ValueError(
            f"Benchmark {benchmark_dir} does not have delta mode (ref_commit is missing)"
        )
    if not repo_info.base_commit:
        raise ValueError(
            f"Benchmark {benchmark_dir} does not have base_commit in delta mode"
        )

    if verbose:
        logger.info(f"📦 Processing benchmark: {benchmark_dir}")
        logger.info(f"   base_commit: {repo_info.base_commit[:8]}")
        logger.info(f"   ref_commit: {repo_info.ref_commit[:8]}")

    # Ensure repository is cloned (needed to generate diff)
    # Use base_commit since we need the repo at that point to generate diff
    project_dir = ensure_project_repository(
        benchmark_dir=benchmark_dir,
        repos_dir=repos_dir,
        commit=repo_info.base_commit,
        verbose=verbose,
    )

    if not project_dir:
        raise RuntimeError(f"Failed to clone repository for {benchmark_dir}")

    # Write diff using repo_info
    return write_diff_from_repo_info(
        repo_info=repo_info,
        repo_dir=project_dir,
        output_path=output_path,
        verbose=verbose,
    )


def clone_repository(
    repo_url: str,
    target_dir: str,
    commit: Optional[str] = None,
    repos_dir: Optional[str] = None,
    *,
    verbose: bool = False,
) -> bool:
    """
    Clone a git repository and optionally checkout a specific commit.

    Uses reference repository pattern (--reference --dissociate) when
    USE_REFERENCE_REPOS is enabled to share git objects and reduce storage.

    Args:
        repo_url: Git repository URL
        target_dir: Directory to clone into
        commit: Optional commit hash to checkout
        repos_dir: Directory for reference repos (for --reference pattern)
        verbose: Enable verbose logging

    Returns:
        True if successful, False otherwise
    """
    target_path = Path(target_dir)
    repo_name = derive_repo_name_from_url(repo_url)
    short_commit = commit[:8] if commit else "HEAD"

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
                logger.warning(f"Error resetting repository: {e}")

            if verbose:
                logger.info(f"{target_dir} is already a git repository")
            return True
        logger.error(f"{target_dir} exists but is not a git repository")
        return False

    # Create parent directory if needed
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Clone repository
    try:
        start_time = time.time()

        # Determine if we should use reference repository
        use_reference = USE_REFERENCE_REPOS and repos_dir is not None
        reference_repo: Optional[str] = None

        if use_reference and repos_dir is not None:
            reference_repo = ensure_reference_repo(repo_url, repos_dir, verbose=verbose)
            if reference_repo:
                logger.info(
                    f"Cloning {repo_name}@{short_commit} with reference → {target_dir}"
                )
                clone_args = [
                    "clone",
                    "--reference",
                    reference_repo,
                    "--dissociate",
                    repo_url,
                    str(target_dir),
                ]
            else:
                logger.warning(
                    "Reference repo unavailable, falling back to standard clone"
                )
                use_reference = False

        if not use_reference:
            logger.info(f"Cloning {repo_name}@{short_commit} → {target_dir}")
            clone_args = ["clone", repo_url, str(target_dir)]

        result = run_git(
            clone_args,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            check=False,
        )

        elapsed = time.time() - start_time

        if result.returncode != 0:
            logger.error(
                f"Clone failed for {repo_name}@{short_commit} "
                f"after {elapsed:.1f}s: {result.stderr}"
            )
            return False

        clone_mode = "with reference" if use_reference else "standard"
        logger.info(
            f"Cloned {repo_name}@{short_commit} in {elapsed:.1f}s ({clone_mode})"
        )

        # Checkout specific commit if provided
        if commit:
            checkout_start = time.time()
            if verbose:
                logger.info(f"Checking out {repo_name}@{commit[:8]}...")

            result = run_git(
                ["checkout", commit],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            checkout_elapsed = time.time() - checkout_start

            if result.returncode != 0:
                logger.error(
                    f"Checkout failed for {commit[:8]} "
                    f"after {checkout_elapsed:.1f}s: {result.stderr}"
                )
                logger.warning("Repository cloned but commit checkout failed")
                return True  # Still return True as clone succeeded

            if verbose:
                logger.info(f"Checked out {commit[:8]} in {checkout_elapsed:.1f}s")

        # Initialize submodules if present (some projects like shadowsocks need this)
        gitmodules = target_path / ".gitmodules"
        if gitmodules.exists():
            if verbose:
                logger.info("Initializing git submodules...")

            result = run_git(
                ["submodule", "update", "--init", "--recursive"],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for submodules
                check=False,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to initialize submodules: {result.stderr}")
                # Don't fail - submodules might not be required
            elif verbose:
                logger.info("Submodules initialized")

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Clone timed out for {repo_name}")
        return False
    except Exception as e:
        logger.error(f"Error cloning {repo_name}: {e}")
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
            logger.debug(f"Derived repo_name from URL: {repo_name}")

    # Create commit-specific directory: {repo_name}-{short_commit}
    short_commit = target_commit[:8]
    commit_specific_name = f"{repo_name}-{short_commit}"
    target_dir = repos_dir_path / commit_specific_name

    if verbose:
        logger.debug(f"Using commit-specific directory: {target_dir}")

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
    - Cache stores pristine repos at specific commits (read-only)
    - Always copies to target_dir (never operates on cache directly)
    - If cache exists: copy to target_dir
    - If no cache: clone to target_dir, then populate cache

    Args:
        repo_url: Repository URL to clone from
        commit: Commit hash to checkout
        target_dir: Target directory for the repository (should be unique per operation)
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

    # Get lock for the cache directory to prevent concurrent access
    cache_lock = _get_repo_lock(cache_dir)

    # Check if cache exists and is valid (read-only verification)
    cache_verified = False
    with cache_lock:
        if Path(cache_dir).is_dir() and (Path(cache_dir) / ".git").exists():
            try:
                # Just verify commit - cache is read-only, no reset
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
                            logger.debug(f"Cache verified: {cache_dir}")
                    else:
                        logger.warning(
                            f"⚠️  Cache at wrong commit: {current_commit[:8]} != {commit[:8]}, removing"
                        )
                        shutil.rmtree(cache_dir)
            except Exception as e:
                logger.warning(f"Failed to verify cache: {e}")
                try:
                    shutil.rmtree(cache_dir)
                except Exception:
                    pass

    # If target already exists and is a valid git repo, reuse it directly
    target_path = Path(target_dir)
    if target_path.is_dir() and (target_path / ".git").exists():
        reset_and_clean_repo(target_dir, verbose=verbose)
        if verbose:
            logger.debug(f"Reusing existing repo at {target_dir}")
        return target_dir

    # If cache verified, copy to target
    if cache_verified:
        derived_name = repo_name or derive_repo_name_from_url(repo_url)
        start_time = time.time()
        try:
            with cache_lock:
                shutil.copytree(
                    cache_dir, target_dir, symlinks=True, ignore_dangling_symlinks=True
                )
            # Reset target to fix stale git index from copytree
            reset_and_clean_repo(target_dir, verbose=verbose)
            elapsed = time.time() - start_time
            logger.info(
                f"CACHE HIT: {derived_name}@{commit[:8]} → {target_dir} ({elapsed:.1f}s)"
            )
            return target_dir
        except Exception as e:
            logger.warning(f"Failed to copy from cache: {e}, will clone")
            # Fall through to clone

    # No cache - clone directly to target
    # If target_dir == cache_dir, we need to protect the clone with a lock
    # to prevent parallel workers from cloning to the same directory
    target_is_cache = str(Path(target_dir).resolve()) == str(Path(cache_dir).resolve())

    derived_name = repo_name or derive_repo_name_from_url(repo_url)
    logger.info(f"CACHE MISS: {derived_name}@{commit[:8]} (expected at {cache_dir})")

    if target_is_cache:
        # Clone to cache directory with lock protection
        with cache_lock:
            # Double-check: another worker might have created cache while we waited
            if Path(cache_dir).is_dir() and (Path(cache_dir) / ".git").exists():
                try:
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
                            if verbose:
                                logger.info(
                                    f"✅ Cache created by another worker: {cache_dir}"
                                )
                            reset_and_clean_repo(cache_dir, verbose=verbose)
                            return cache_dir
                except Exception:
                    pass

            if verbose:
                logger.info(f"📦 No cache available, cloning to {target_dir}...")

            # Determine repos_dir for reference repo
            effective_repos_dir = repos_dir
            if not effective_repos_dir:
                crsbench_root = Path(__file__).parent.parent.parent.resolve()
                effective_repos_dir = str(
                    Path(
                        os.getenv(
                            "PROJECT_REPOS_DIR", str(crsbench_root / ".crsbench-repos")
                        )
                    )
                )

            success = clone_repository(
                repo_url=repo_url,
                target_dir=target_dir,
                commit=commit,
                repos_dir=effective_repos_dir,
                verbose=verbose,
            )

            if not success:
                return None

            return target_dir
    else:
        # Clone to separate target_dir (not the cache)
        if verbose:
            logger.info(f"📦 No cache available, cloning to {target_dir}...")

        # Determine repos_dir for reference repo
        effective_repos_dir = repos_dir
        if not effective_repos_dir:
            crsbench_root = Path(__file__).parent.parent.parent.resolve()
            effective_repos_dir = str(
                Path(
                    os.getenv(
                        "PROJECT_REPOS_DIR", str(crsbench_root / ".crsbench-repos")
                    )
                )
            )

        success = clone_repository(
            repo_url=repo_url,
            target_dir=target_dir,
            commit=commit,
            repos_dir=effective_repos_dir,
            verbose=verbose,
        )

        if not success:
            return None

        # Populate cache for future uses (protected by lock)
        with cache_lock:
            if not Path(cache_dir).is_dir():
                if verbose:
                    logger.info(f"📦 Populating cache: {target_dir} -> {cache_dir}")
                try:
                    shutil.copytree(
                        target_dir,
                        cache_dir,
                        symlinks=True,
                        ignore_dangling_symlinks=True,
                    )
                    if verbose:
                        logger.info(f"✅ Cache created at {cache_dir}")
                except Exception as e:
                    logger.warning(f"⚠️  Failed to create cache: {e}")

        return target_dir


def ensure_project_repository(
    benchmark_dir: str,
    repos_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    commit: Optional[str] = None,
    *,
    mode: Optional[str] = None,
    verbose: bool = False,
    remove_aixcc: bool = False,
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
        mode: Evaluation mode ('delta' or 'full') for selecting the correct commit
        verbose: Enable verbose logging
        remove_aixcc: Remove .aixcc directory from cloned source (prevents CRS accessing ground truth)

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

    # This function always clones from main_repo.
    # For bundled pkgs/, use load_benchmark_source(source_mode="pkgs") instead.

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
        repo_info = get_repo_info_from_benchmark(benchmark_dir, mode=mode)
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
        # Use ref_commit for delta mode, base_commit for full mode
        if repo_info.ref_commit:
            # Delta mode: use ref_commit (patched version)
            target_commit = repo_info.ref_commit
            if verbose:
                logger.info(f"Using ref_commit from meta.yaml: {target_commit[:8]}")
        elif repo_info.base_commit:
            # Full mode: use base_commit
            target_commit = repo_info.base_commit
            if verbose:
                logger.info(f"Using base_commit from meta.yaml: {target_commit[:8]}")
        else:
            logger.error(f"❌ No commit found in meta.yaml for {benchmark_dir}")
            return None

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
    result = clone_or_copy_cached_repo(
        repo_url=repo_info.repo_url,
        commit=target_commit,
        target_dir=target_dir,
        repos_dir=repos_dir,
        repo_name=repo_info.repo_name,
        verbose=verbose,
    )

    # Remove .aixcc directory if requested
    if result and remove_aixcc:
        aixcc_dir = Path(result) / ".aixcc"
        if aixcc_dir.exists():
            logger.warning(f"Removing .aixcc directory from source: {aixcc_dir}")
            shutil.rmtree(aixcc_dir)

    return result


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
