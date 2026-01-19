"""Tests for the tarball module."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


class TestCopytreeWithSymlinks:
    """Test that copytree handles symlinks correctly."""

    def test_copytree_with_symlinks_preserves_links(self):
        """Test that copytree with symlinks=True preserves symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create source directory with symlink
            src_dir = work_dir / "src"
            src_dir.mkdir()
            (src_dir / "real_file.txt").write_text("content")
            (src_dir / "link_file.txt").symlink_to("real_file.txt")

            # Copy with symlinks=True
            dst_dir = work_dir / "dst"
            shutil.copytree(src_dir, dst_dir, symlinks=True)

            # Verify symlink is preserved
            link_path = dst_dir / "link_file.txt"
            assert link_path.is_symlink()
            assert link_path.read_text() == "content"

    def test_copytree_without_symlinks_follows_links(self):
        """Test that copytree without symlinks=True follows symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create source directory with symlink
            src_dir = work_dir / "src"
            src_dir.mkdir()
            (src_dir / "real_file.txt").write_text("content")
            (src_dir / "link_file.txt").symlink_to("real_file.txt")

            # Copy without symlinks=True (default behavior)
            dst_dir = work_dir / "dst"
            shutil.copytree(src_dir, dst_dir)

            # Verify symlink is followed (becomes regular file)
            link_path = dst_dir / "link_file.txt"
            assert not link_path.is_symlink()
            assert link_path.read_text() == "content"

    def test_copytree_broken_symlink_without_flag_raises(self):
        """Test that copytree fails on broken symlinks without symlinks=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create source directory with broken symlink
            src_dir = work_dir / "src"
            src_dir.mkdir()
            (src_dir / "broken_link.txt").symlink_to("nonexistent.txt")

            # Copy without symlinks=True should fail
            dst_dir = work_dir / "dst"
            with pytest.raises(shutil.Error):
                shutil.copytree(src_dir, dst_dir)

    def test_copytree_broken_symlink_with_flag_succeeds(self):
        """Test that copytree with symlinks=True handles broken symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create source directory with broken symlink
            src_dir = work_dir / "src"
            src_dir.mkdir()
            (src_dir / "broken_link.txt").symlink_to("nonexistent.txt")

            # Copy with symlinks=True should succeed
            dst_dir = work_dir / "dst"
            shutil.copytree(src_dir, dst_dir, symlinks=True)

            # Verify broken symlink is preserved
            link_path = dst_dir / "broken_link.txt"
            assert link_path.is_symlink()


class TestGenerateRefDiff:
    """Test ref.diff generation with symlinks."""

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_generate_ref_diff_with_symlinks(self):
        """Test that _generate_ref_diff works with repos containing symlinks."""
        import os

        from crsbench.benchmark.packaging.tarball import _generate_ref_diff

        # Git environment for commits (disable GPG signing)
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

        def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            # Add -c commit.gpgsign=false to disable GPG signing
            full_args = ["-c", "commit.gpgsign=false", *args]
            return subprocess.run(
                ["git", *full_args],
                cwd=cwd,
                capture_output=True,
                text=True,
                env=git_env,
                check=True,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Create a git repo with symlinks
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # Create initial commit (base)
            (repo_dir / "main.c").write_text("int main() { return 0; }")
            # Create a symlink to main.c
            (repo_dir / "link.c").symlink_to("main.c")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create second commit (ref) with a change
            (repo_dir / "main.c").write_text("int main() { return 1; }")  # Change
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Change"], cwd=repo_dir)
            ref_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Generate ref.diff
            output_dir = work_dir / "output"
            output_dir.mkdir()

            diff_path = _generate_ref_diff(
                repo_dir=repo_dir,
                base_commit=base_commit,
                ref_commit=ref_commit,
                work_dir=work_dir / "work",
                output_dir=output_dir,
            )

            # Verify diff was generated
            assert diff_path.exists()
            diff_content = diff_path.read_text()
            assert "return 0" in diff_content or "return 1" in diff_content
