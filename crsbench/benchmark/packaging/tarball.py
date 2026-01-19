"""Create source tarballs with fresh git init.

Ported from Team-Atlanta/generate-challenge-task repotar() function.
Creates reproducible tarballs with:
- Fresh git init (single commit, no history)
- Cleaned source (no .git, .github, .aixcc)
- Proper directory structure matching Dockerfile WORKDIR
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def create_source_tarball(
    repo_url: str,
    base_commit: str,
    source_name: str,
    output_dir: Path,
    *,
    ref_commit: Optional[str] = None,
) -> tuple[Path, Optional[Path]]:
    """Create source tarball with fresh git init.

    Args:
        repo_url: Git repository URL
        base_commit: Commit to checkout for base source
        source_name: Directory name in tarball (from Dockerfile WORKDIR)
        output_dir: Directory to write tarball and ref.diff
        ref_commit: If provided, generate ref.diff between base and ref

    Returns:
        Tuple of (tarball_path, ref_diff_path or None)

    Raises:
        subprocess.CalledProcessError: If git operations fail
        RuntimeError: If source preparation fails
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)

        # 1. Clone repository
        logger.info(f"Cloning {repo_url}...")
        _run_git(["clone", repo_url, "repo"], cwd=work_dir)
        repo_dir = work_dir / "repo"

        # 2. Generate ref.diff for delta mode (before modifying repo)
        ref_diff_path = None
        if ref_commit:
            ref_diff_path = _generate_ref_diff(
                repo_dir=repo_dir,
                base_commit=base_commit,
                ref_commit=ref_commit,
                work_dir=work_dir,
                output_dir=output_dir,
            )

        # 3. Checkout base commit for source tarball
        _run_git(["checkout", base_commit], cwd=repo_dir)

        # 4. Clean up - remove git metadata and sensitive directories
        _clean_source(repo_dir)

        # 5. Fresh git init (CRS needs git commands to work)
        _fresh_git_init(repo_dir)

        # 6. Rename to expected name and create tarball
        source_dir = work_dir / source_name
        repo_dir.rename(source_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        tarball_path = output_dir / f"{source_name}.tar.gz"

        # Use --warning=no-file-changed to handle race conditions with git objects
        # tar returns exit code 1 when files change during archiving, which is okay
        result = subprocess.run(
            [
                "tar",
                "--warning=no-file-changed",
                "-czf",
                str(tarball_path),
                source_name,
            ],
            cwd=work_dir,
            capture_output=True,
        )
        # Exit code 1 with "file changed" is acceptable, only fail on other errors
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )

        logger.info(f"Created tarball: {tarball_path}")
        return tarball_path, ref_diff_path


def _generate_ref_diff(
    repo_dir: Path,
    base_commit: str,
    ref_commit: str,
    work_dir: Path,
    output_dir: Path,
) -> Path:
    """Generate ref.diff between base and ref commits.

    Uses git diff --no-index to create a clean diff without git history.
    """
    logger.info(f"Generating ref.diff: {base_commit[:8]}..{ref_commit[:8]}")

    # Checkout base version
    # Use symlinks=True to preserve symlinks (some repos like mongoose have many)
    base_dir = work_dir / "base"
    shutil.copytree(repo_dir, base_dir, symlinks=True)
    _run_git(["checkout", base_commit], cwd=base_dir)
    _clean_source(base_dir)

    # Checkout ref version
    ref_dir = work_dir / "ref"
    shutil.copytree(repo_dir, ref_dir, symlinks=True)
    _run_git(["checkout", ref_commit], cwd=ref_dir)
    _clean_source(ref_dir)

    # Generate diff with git diff --no-index
    # Note: git diff --no-index returns exit code 1 when there are differences
    result = subprocess.run(
        ["git", "diff", "--no-index", str(base_dir), str(ref_dir)],
        capture_output=True,
        text=True,
    )

    # Clean up paths in diff (a/tmp/xxx/base/ -> a/, b/tmp/xxx/ref/ -> b/)
    diff_content = result.stdout
    diff_content = diff_content.replace(f"a{base_dir}/", "a/")
    diff_content = diff_content.replace(f"b{ref_dir}/", "b/")

    output_dir.mkdir(parents=True, exist_ok=True)
    ref_diff_path = output_dir / "ref.diff"
    ref_diff_path.write_text(diff_content)

    logger.info(f"Generated ref.diff: {ref_diff_path} ({len(diff_content)} bytes)")
    return ref_diff_path


def _clean_source(directory: Path) -> None:
    """Remove git metadata and sensitive directories from source."""
    cleanup_dirs = [".git", ".github", ".aixcc"]
    for cleanup_dir in cleanup_dirs:
        cleanup_path = directory / cleanup_dir
        if cleanup_path.exists():
            shutil.rmtree(cleanup_path)


def _fresh_git_init(directory: Path) -> None:
    """Initialize fresh git repo with single commit.

    Uses fixed author/committer for reproducibility.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CRSBench",
        "GIT_AUTHOR_EMAIL": "crsbench@example.com",
        "GIT_COMMITTER_NAME": "CRSBench",
        "GIT_COMMITTER_EMAIL": "crsbench@example.com",
        # Use fixed date for reproducibility
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
    }

    subprocess.run(
        ["git", "init"],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", "Initial source"],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    # Pack all loose objects to prevent race conditions during tar
    # This ensures git is done writing and all objects are in packfiles
    subprocess.run(
        ["git", "gc", "--aggressive", "--prune=now"],
        cwd=directory,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run git command with error handling."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args],
            output=result.stdout,
            stderr=result.stderr,
        )
    return result
