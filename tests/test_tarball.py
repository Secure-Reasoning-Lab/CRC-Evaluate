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

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_generate_ref_diff_with_renames(self):
        """Test that _generate_ref_diff cleans up paths for renamed files.

        This test verifies that temp directory paths are stripped from rename
        operations, which would otherwise cause 'inconsistent old filename' errors.
        """
        import os

        from crsbench.benchmark.packaging.tarball import _generate_ref_diff

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

        def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
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

            # Create a git repo
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # Create initial commit (base) with a file to be renamed
            (repo_dir / "old_name.c").write_text("int old() { return 0; }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create second commit (ref) with renamed file
            run_git(["mv", "old_name.c", "new_name.c"], cwd=repo_dir)
            run_git(["commit", "-m", "Rename file"], cwd=repo_dir)
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

            # Verify diff was generated and paths are clean
            assert diff_path.exists()
            diff_content = diff_path.read_text()

            # Should have clean rename paths (no temp dir)
            assert "rename from old_name.c" in diff_content
            assert "rename to new_name.c" in diff_content

            # Should NOT have temp directory paths
            assert "/tmp/" not in diff_content
            assert "base/" not in diff_content
            assert "ref/" not in diff_content


class TestCRLFLineEndings:
    """Test that CRLF line endings are preserved in tarball and ref.diff."""

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_crlf_preserved_in_ref_diff(self):
        """Test that files with CRLF line endings produce matching diff and tarball.

        This test simulates repos like curl that have CRLF line endings in test files.
        The diff should preserve CRLF so it can be applied to the tarball.
        """
        import os

        from crsbench.benchmark.packaging.tarball import (
            _clean_source,
            _fresh_git_init,
            _generate_ref_diff,
        )

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

        def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
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

            # Create a git repo with CRLF line endings
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)
            # Disable autocrlf in the test repo
            run_git(["config", "core.autocrlf", "false"], cwd=repo_dir)

            # Create file with CRLF line endings (like curl test files)
            crlf_content_base = "Line 1\r\nLine 2\r\nLine 3\r\n"
            (repo_dir / "test_data.txt").write_bytes(crlf_content_base.encode())
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial with CRLF"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create second commit with change to CRLF file
            crlf_content_ref = "Line 1\r\nLine 2 MODIFIED\r\nLine 3\r\n"
            (repo_dir / "test_data.txt").write_bytes(crlf_content_ref.encode())
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Modify CRLF file"], cwd=repo_dir)
            ref_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Generate ref.diff (simulates _generate_ref_diff)
            output_dir = work_dir / "output"
            output_dir.mkdir()

            diff_path = _generate_ref_diff(
                repo_dir=repo_dir,
                base_commit=base_commit,
                ref_commit=ref_commit,
                work_dir=work_dir / "diff_work",
                output_dir=output_dir,
            )

            # Verify CRLF is preserved in the generated diff
            diff_content = diff_path.read_bytes()
            assert b"\r\n" in diff_content, "CRLF should be preserved in ref.diff"

            # Create tarball source (simulates create_source_tarball)
            tarball_work = work_dir / "tarball_work"
            tarball_work.mkdir()
            tarball_repo = tarball_work / "repo"
            shutil.copytree(repo_dir, tarball_repo, symlinks=True)
            run_git(["config", "core.autocrlf", "false"], cwd=tarball_repo)
            run_git(["checkout", base_commit], cwd=tarball_repo)
            _clean_source(tarball_repo)
            _fresh_git_init(tarball_repo)

            # Verify CRLF is preserved in tarball source
            tarball_file = tarball_repo / "test_data.txt"
            tarball_content = tarball_file.read_bytes()
            assert b"\r\n" in tarball_content, "CRLF should be preserved in tarball"

            # Apply ref.diff to tarball source
            # _fresh_git_init already created a git repo with initial commit
            result = subprocess.run(
                ["git", "apply", str(diff_path)],
                cwd=tarball_repo,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"git apply failed: {result.stderr}"

            # Verify the patch was applied correctly
            patched_content = (tarball_repo / "test_data.txt").read_bytes()
            assert b"Line 2 MODIFIED\r\n" in patched_content

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_lf_files_still_work(self):
        """Test that normal LF files still work correctly."""
        import os

        from crsbench.benchmark.packaging.tarball import _generate_ref_diff

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }

        def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
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

            # Create a git repo with LF line endings
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)
            run_git(["config", "core.autocrlf", "false"], cwd=repo_dir)

            # Create file with LF line endings (normal Unix files)
            lf_content_base = "Line 1\nLine 2\nLine 3\n"
            (repo_dir / "source.c").write_text(lf_content_base)
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial with LF"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create second commit
            lf_content_ref = "Line 1\nLine 2 CHANGED\nLine 3\n"
            (repo_dir / "source.c").write_text(lf_content_ref)
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Modify LF file"], cwd=repo_dir)
            ref_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Generate ref.diff
            output_dir = work_dir / "output"
            output_dir.mkdir()

            diff_path = _generate_ref_diff(
                repo_dir=repo_dir,
                base_commit=base_commit,
                ref_commit=ref_commit,
                work_dir=work_dir / "diff_work",
                output_dir=output_dir,
            )

            # Verify diff was generated and contains LF content
            assert diff_path.exists()
            diff_content = diff_path.read_text()
            assert "Line 2" in diff_content
            assert "Line 2 CHANGED" in diff_content
