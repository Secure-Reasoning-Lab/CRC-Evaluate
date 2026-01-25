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
    """Create source tarball with single squashed commit.

    Both modes produce a tarball with 1 squashed commit at the vulnerable state:
    - Delta mode (ref_commit provided): 1 commit at ref_commit + ref.diff as hint
    - Full-only mode (no ref_commit): 1 commit at base_commit (vulnerable state)

    The ref.diff hint file is the canonical way for CRS to discover changes
    in delta mode. Git history is not used to expose the vulnerability.

    Args:
        repo_url: Git repository URL
        base_commit: Commit for full mode (vulnerable state) or delta mode base
        source_name: Directory name in tarball (from Dockerfile WORKDIR)
        output_dir: Directory to write tarball and ref.diff
        ref_commit: If provided, generate ref.diff and use ref_commit as vulnerable state

    Returns:
        Tuple of (tarball_path, ref_diff_path or None)

    Raises:
        subprocess.CalledProcessError: If git operations fail
        RuntimeError: If source preparation fails
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)

        # 1. Clone repository
        logger.info(f"Cloning {repo_url} to {work_dir}...")
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

        # 3. Disable autocrlf to preserve original line endings (CRLF or LF)
        # This ensures tarball content matches ref.diff exactly
        _run_git(["config", "core.autocrlf", "false"], cwd=repo_dir)

        # 4. Prepare source with single squashed commit at vulnerable state
        # Delta mode: ref_commit is vulnerable state
        # Full mode: base_commit is vulnerable state
        vulnerable_commit = ref_commit if ref_commit else base_commit
        _prepare_source(repo_dir, vulnerable_commit)

        # 4. Rename to expected name and create tarball
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


def _prepare_source(repo_dir: Path, target_commit: str) -> None:
    """Prepare source with single squashed commit at target state.

    Creates a single commit at the specified commit (vulnerable state).
    CRS cannot use git history to discover what changed - ref.diff is the
    canonical hint for delta mode.

    Args:
        repo_dir: Path to cloned repository
        target_commit: Commit to checkout (vulnerable state)
    """
    logger.info(f"Creating 1-commit tarball at {target_commit[:8]}")

    # 1. Checkout target commit (vulnerable state)
    _run_git(["checkout", target_commit], cwd=repo_dir)

    # Initialize submodules
    if (repo_dir / ".gitmodules").exists():
        logger.info("Initializing git submodules...")
        _run_git(["submodule", "update", "--init", "--recursive"], cwd=repo_dir)

    # 2. Clean and create single squashed commit
    _clean_source(repo_dir)
    _fresh_git_init(repo_dir)

    logger.info("Created 1-commit tarball")


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
    # Disable autocrlf to preserve original line endings (CRLF or LF)
    # This ensures diff matches tarball content exactly
    _run_git(["config", "core.autocrlf", "false"], cwd=base_dir)
    _run_git(["checkout", base_commit], cwd=base_dir)
    # Initialize submodules if present
    if (base_dir / ".gitmodules").exists():
        _run_git(["submodule", "update", "--init", "--recursive"], cwd=base_dir)
    _clean_source(base_dir)

    # Checkout ref version
    ref_dir = work_dir / "ref"
    shutil.copytree(repo_dir, ref_dir, symlinks=True)
    _run_git(["config", "core.autocrlf", "false"], cwd=ref_dir)
    _run_git(["checkout", ref_commit], cwd=ref_dir)
    # Initialize submodules if present
    if (ref_dir / ".gitmodules").exists():
        _run_git(["submodule", "update", "--init", "--recursive"], cwd=ref_dir)
    _clean_source(ref_dir)

    # Generate diff with git diff --no-index
    # --binary: Include binary file content (needed for .zip, .png, etc.)
    # --full-index: Use full SHA-1 hashes (required for applying binary patches)
    # Note: git diff --no-index returns exit code 1 when there are differences
    # Use binary mode to preserve CRLF line endings in the diff output
    # text=True would normalize line endings, breaking CRLF files like curl's test data
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--binary",
            "--full-index",
            str(base_dir),
            str(ref_dir),
        ],
        capture_output=True,
    )

    # Clean up temp directory paths from diff output
    # git diff --no-index produces paths like:
    #   diff --git a/tmp/base/file b/tmp/ref/file
    #   --- a/tmp/base/file
    #   +++ b/tmp/ref/file
    #   rename from tmp/base/old
    #   rename to tmp/ref/new
    #   copy from tmp/base/old
    #   copy to tmp/ref/new
    #
    # We need to strip the temp dir prefixes to get clean relative paths.
    # Handle both base_dir and ref_dir since git may use either depending on
    # whether a file is new, deleted, renamed, or modified.
    diff_content = result.stdout
    base_prefix = str(base_dir).encode()
    ref_prefix = str(ref_dir).encode()

    # Pattern replacements: (search_pattern, replacement)
    # Order matters - more specific patterns first
    replacements = [
        # a/prefix and b/prefix patterns (for diff --git, ---, +++ lines)
        (b"a" + base_prefix + b"/", b"a/"),
        (b"b" + ref_prefix + b"/", b"b/"),
        (b"a" + ref_prefix + b"/", b"a/"),  # new files: both point to ref
        (b"b" + base_prefix + b"/", b"b/"),  # deleted files: both point to base
        # rename/copy patterns (no a/ or b/ prefix)
        (b"rename from " + base_prefix + b"/", b"rename from "),
        (b"rename to " + ref_prefix + b"/", b"rename to "),
        (b"rename from " + ref_prefix + b"/", b"rename from "),
        (b"rename to " + base_prefix + b"/", b"rename to "),
        (b"copy from " + base_prefix + b"/", b"copy from "),
        (b"copy to " + ref_prefix + b"/", b"copy to "),
        (b"copy from " + ref_prefix + b"/", b"copy from "),
        (b"copy to " + base_prefix + b"/", b"copy to "),
    ]

    for pattern, replacement in replacements:
        diff_content = diff_content.replace(pattern, replacement)

    output_dir.mkdir(parents=True, exist_ok=True)
    ref_diff_path = output_dir / "ref.diff"
    ref_diff_path.write_bytes(diff_content)

    logger.info(f"Generated ref.diff: {ref_diff_path} ({len(diff_content)} bytes)")
    return ref_diff_path


def _clean_source(directory: Path) -> None:
    """Remove git metadata and sensitive directories from source.

    Recursively removes .git directories/files from submodules too.
    """
    # Remove top-level sensitive directories
    for cleanup_dir in [".github", ".aixcc"]:
        cleanup_path = directory / cleanup_dir
        if cleanup_path.exists():
            shutil.rmtree(cleanup_path)

    # Recursively remove all .git directories and files (including submodules)
    # Submodules have .git as a file pointing to parent's .git/modules/
    for git_path in directory.rglob(".git"):
        if git_path.is_file():
            git_path.unlink()
        elif git_path.is_dir():
            shutil.rmtree(git_path)


def _fresh_git_init(
    directory: Path,
    commit_message: str = "Initial source",
    *,
    run_gc: bool = True,
) -> None:
    """Initialize fresh git repo with single commit.

    Args:
        directory: Path to directory to initialize
        commit_message: Message for the initial commit
        run_gc: If True, run git gc to pack objects (set False if more commits follow)

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
    # Disable auto-gc to prevent conflict with our explicit gc below
    subprocess.run(
        ["git", "config", "gc.auto", "0"],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", commit_message],
        cwd=directory,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )

    if run_gc:
        _run_git_gc(directory)


def _run_git_gc(directory: Path) -> None:
    """Pack all loose objects to prevent race conditions during tar."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CRSBench",
        "GIT_AUTHOR_EMAIL": "crsbench@example.com",
        "GIT_COMMITTER_NAME": "CRSBench",
        "GIT_COMMITTER_EMAIL": "crsbench@example.com",
    }
    result = subprocess.run(
        ["git", "gc", "--aggressive", "--prune=now"],
        cwd=directory,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if result.returncode != 0:
        stderr_msg = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git gc failed (exit {result.returncode}): {stderr_msg}")


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
