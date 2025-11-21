"""
Repository manager for automatic cloning of project repositories.

This module handles automatic cloning and checkout of project repositories
needed for test.sh generation.
"""

import os
import subprocess
import yaml
from pathlib import Path
from typing import Optional, Dict, Any
from crsbench.utils.logger import get_logger, configure_logger

logger = get_logger(__name__)


def get_repo_info_from_benchmark(benchmark_dir: str) -> Dict[str, Any]:
    """
    Get repository information from benchmark configuration.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        Dictionary with repo_url, repo_name (optional), base_commit, ref_commit

    Raises:
        FileNotFoundError: If configuration files are not found
        ValueError: If required fields are missing
    """
    benchmark_path = Path(benchmark_dir)

    # Read project.yaml for main_repo URL
    project_yaml = benchmark_path / "project.yaml"
    if not project_yaml.exists():
        raise FileNotFoundError(f"project.yaml not found in {benchmark_dir}")

    with open(project_yaml) as f:
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

    with open(meta_yaml) as f:
        meta_config = yaml.safe_load(f)

    # Get base_commit from delta_mode or full_mode
    base_commit = None
    ref_commit = None

    if "delta_mode" in meta_config:
        base_commit = meta_config["delta_mode"].get("base_commit")
        ref_commit = meta_config["delta_mode"].get("ref_commit")
    elif "full_mode" in meta_config:
        base_commit = meta_config["full_mode"].get("base_commit")

    return {
        "repo_url": repo_url,
        "repo_name": repo_name,
        "base_commit": base_commit,
        "ref_commit": ref_commit,
    }


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


def clone_repository(
    repo_url: str,
    target_dir: str,
    commit: Optional[str] = None,
    verbose: bool = False
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
            logger.info(f"Directory {target_dir} already exists, checking if it's a git repo...")

        # Check if it's a git repository
        if (target_path / ".git").exists():
            if verbose:
                logger.info(f"✅ {target_dir} is already a git repository")
            return True
        else:
            logger.error(f"❌ {target_dir} exists but is not a git repository")
            return False

    # Create parent directory if needed
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Clone repository
    try:
        if verbose:
            logger.info(f"🔄 Cloning {repo_url} to {target_dir}...")

        cmd = ["git", "clone", repo_url, str(target_dir)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode != 0:
            logger.error(f"❌ Git clone failed: {result.stderr}")
            return False

        if verbose:
            logger.info(f"✅ Successfully cloned repository")

        # Checkout specific commit if provided
        if commit:
            if verbose:
                logger.info(f"🔄 Checking out commit {commit}...")

            cmd = ["git", "checkout", commit]
            result = subprocess.run(
                cmd,
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.error(f"⚠️  Failed to checkout {commit}: {result.stderr}")
                logger.warning("Repository cloned but commit checkout failed")
                return True  # Still return True as clone succeeded

            if verbose:
                logger.info(f"✅ Checked out commit {commit}")

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"❌ Git clone timed out")
        return False
    except Exception as e:
        logger.error(f"❌ Error cloning repository: {e}")
        return False


def ensure_project_repository(
    benchmark_dir: str,
    repos_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    verbose: bool = False
) -> Optional[str]:
    """
    Ensure project repository exists at commit-specific directory, cloning if necessary.

    This function uses commit-specific directories for parallel execution safety
    and cache efficiency. Directory structure: {repos_dir}/{repo_name}-{short_commit}

    Args:
        benchmark_dir: Path to benchmark directory
        repos_dir: Directory to store cloned repositories (default: PROJECT_REPOS_DIR env var or .crsbench-repos)
        project_dir: Explicit project directory path (if provided, this is used directly)
        verbose: Enable verbose logging

    Returns:
        Path to project directory, or None if failed

    Workflow:
        1. If project_dir is provided and exists, return it
        2. If project_dir is provided but doesn't exist, try to clone there
        3. If project_dir is not provided, use commit-specific directory in repos_dir
    """
    # Setup logging
    if verbose:
        configure_logger(level="INFO")

    # If explicit project_dir is provided
    if project_dir:
        if os.path.isdir(project_dir):
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

    # Determine target directory for clone
    if project_dir:
        # Use the specified project_dir
        target_dir = project_dir
    else:
        # Use commit-specific directory for cache efficiency and parallel safety
        if not repos_dir:
            # Try PROJECT_REPOS_DIR env var first, then default to .crsbench-repos
            crsbench_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
            default_repos_dir = os.path.join(crsbench_root, '.crsbench-repos')
            repos_dir = os.getenv("PROJECT_REPOS_DIR", default_repos_dir)

        # Use explicit repo_name if provided, otherwise derive from URL
        if repo_info.get("repo_name"):
            repo_name = repo_info["repo_name"]
            if verbose:
                logger.info(f"Using explicit repo_name from project.yaml: {repo_name}")
        else:
            repo_name = derive_repo_name_from_url(repo_info["repo_url"])
            if verbose:
                logger.info(f"Derived repo_name from URL: {repo_name}")

        # Get base_commit and create commit-specific directory name
        base_commit = repo_info.get("base_commit")
        if not base_commit:
            logger.error(f"❌ No base_commit found in meta.yaml for {benchmark_dir}")
            return None

        # Create commit-specific directory: {repo_name}-{short_commit}
        short_commit = base_commit[:8]
        commit_specific_name = f"{repo_name}-{short_commit}"
        target_dir = os.path.join(repos_dir, commit_specific_name)

        if verbose:
            logger.info(f"Using commit-specific directory: {target_dir}")

    # Check if directory already exists and has correct commit
    if os.path.isdir(target_dir):
        # Verify it's at the correct commit
        try:
            from pathlib import Path
            if (Path(target_dir) / ".git").exists():
                import subprocess
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=target_dir,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    current_commit = result.stdout.strip()
                    expected_commit = repo_info.get("base_commit", "")
                    if current_commit.startswith(expected_commit[:8]):
                        if verbose:
                            logger.info(f"✅ Repository already exists at correct commit: {target_dir}")
                        return target_dir
                    else:
                        logger.warning(f"⚠️  Repository exists but at wrong commit: {current_commit[:8]} != {expected_commit[:8]}")
        except Exception as e:
            logger.warning(f"Failed to verify commit: {e}")

        # If we reach here, directory exists but commit verification failed or mismatched
        # Return existing directory anyway (assume it's usable)
        if verbose:
            logger.info(f"✅ Using existing repository: {target_dir}")
        return target_dir

    # Clone if needed
    if verbose:
        logger.info(f"📦 Repository not found, cloning...")

    success = clone_repository(
        repo_url=repo_info["repo_url"],
        target_dir=target_dir,
        commit=repo_info.get("base_commit"),
        verbose=verbose
    )

    if not success:
        return None

    return target_dir


def find_or_clone_project(
    benchmark_name: str,
    benchmarks_root: str = "benchmarks",
    repos_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    verbose: bool = False
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
    benchmark_dir = os.path.join(benchmarks_root, benchmark_name)

    if not os.path.isdir(benchmark_dir):
        logger.error(f"❌ Benchmark directory not found: {benchmark_dir}")
        return None

    return ensure_project_repository(
        benchmark_dir=benchmark_dir,
        repos_dir=repos_dir,
        project_dir=project_dir,
        verbose=verbose
    )
