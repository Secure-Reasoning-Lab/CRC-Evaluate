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


class TestPrepareSource:
    """Test _prepare_source creates single squashed commit."""

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_prepare_source_has_single_commit(self):
        """Test that _prepare_source creates 1 squashed commit."""
        import os

        from crsbench.benchmark.packaging.tarball import _prepare_source

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

            # Create a git repo with multiple commits
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # First commit
            (repo_dir / "main.c").write_text("int main() { return 0; }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial"], cwd=repo_dir)

            # Second commit (target)
            (repo_dir / "vuln.c").write_text("void vuln() { /* bug */ }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Add vuln"], cwd=repo_dir)
            target_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Clone and prepare
            prepared_dir = work_dir / "prepared"
            shutil.copytree(repo_dir, prepared_dir, symlinks=True)
            _prepare_source(prepared_dir, target_commit)

            # Verify 1 commit
            result = run_git(["rev-list", "--count", "HEAD"], cwd=prepared_dir)
            commit_count = int(result.stdout.strip())
            assert commit_count == 1, f"Expected 1 commit, got {commit_count}"

            # Verify vuln.c exists (target state)
            assert (prepared_dir / "vuln.c").exists()

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_prepare_source_cannot_diff_parent(self):
        """Test that CRS cannot use git diff HEAD~1 (no parent commit)."""
        import os

        from crsbench.benchmark.packaging.tarball import _prepare_source

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

            # Create repo
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            (repo_dir / "main.c").write_text("int main() { return 0; }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial"], cwd=repo_dir)
            target_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Clone and prepare
            prepared_dir = work_dir / "prepared"
            shutil.copytree(repo_dir, prepared_dir, symlinks=True)
            _prepare_source(prepared_dir, target_commit)

            # CRS should NOT be able to diff HEAD~1 (no parent)
            result = subprocess.run(
                ["git", "rev-parse", "HEAD~1"],
                cwd=prepared_dir,
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0, "HEAD~1 should not exist"

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_prepare_source_has_crsbench_author(self):
        """Test that commits show CRSBench as author."""
        import os

        from crsbench.benchmark.packaging.tarball import _prepare_source

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

            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            (repo_dir / "main.c").write_text("int main() { return 0; }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Initial"], cwd=repo_dir)
            target_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            prepared_dir = work_dir / "prepared"
            shutil.copytree(repo_dir, prepared_dir, symlinks=True)
            _prepare_source(prepared_dir, target_commit)

            # Get author info
            result = subprocess.run(
                ["git", "log", "--format=%an <%ae>"],
                cwd=prepared_dir,
                capture_output=True,
                text=True,
            )
            authors = [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]

            for author in authors:
                assert "CRSBench" in author or "crsbench" in author.lower()


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


class TestTarballVulnerableCommit:
    """Test that tarballs contain the correct (vulnerable) commit.

    Both full and delta mode tarballs should contain vulnerable code:
    - Full mode: base_commit is vulnerable
    - Delta mode: ref_commit is vulnerable (base_commit is benign)

    This ensures patches can be applied to fix the vulnerability.
    """

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_full_mode_tarball_at_base_commit(self):
        """Test that full mode creates tarball at base_commit (vulnerable)."""
        import os

        from crsbench.benchmark.packaging.tarball import create_source_tarball

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

            # Create a git repo simulating full mode
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # Create base_commit with vulnerable code (full mode: base is vulnerable)
            (repo_dir / "vuln.c").write_text("void vuln() { /* BUG HERE */ }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Vulnerable code"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create tarball (full mode: no ref_commit)
            output_dir = work_dir / "output"
            output_dir.mkdir()

            tarball_path, ref_diff_path = create_source_tarball(
                repo_url=str(repo_dir),
                base_commit=base_commit,
                source_name="test-source",
                output_dir=output_dir,
                ref_commit=None,  # Full mode: no ref_commit
            )

            # Extract and verify tarball contains vulnerable code
            extract_dir = work_dir / "extract"
            extract_dir.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(tarball_path)],
                cwd=extract_dir,
                check=True,
            )

            vuln_file = extract_dir / "test-source" / "vuln.c"
            assert vuln_file.exists()
            assert "BUG HERE" in vuln_file.read_text()

            # No ref.diff for full mode
            assert ref_diff_path is None

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_delta_mode_tarball_at_ref_commit(self):
        """Test that delta mode creates tarball at ref_commit (vulnerable).

        Delta mode: base_commit is benign, ref_commit introduced the vulnerability.
        Tarball should be at ref_commit so patches can fix it.
        """
        import os

        from crsbench.benchmark.packaging.tarball import create_source_tarball

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

            # Create a git repo simulating delta mode
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # Create base_commit with safe code (delta mode: base is benign)
            (repo_dir / "code.c").write_text("void safe() { /* SAFE CODE */ }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Safe code"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create ref_commit that introduces vulnerability
            (repo_dir / "code.c").write_text("void vuln() { /* BUG INTRODUCED */ }")
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Introduce vulnerability"], cwd=repo_dir)
            ref_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create tarball (delta mode: ref_commit provided)
            output_dir = work_dir / "output"
            output_dir.mkdir()

            tarball_path, ref_diff_path = create_source_tarball(
                repo_url=str(repo_dir),
                base_commit=base_commit,
                source_name="test-source",
                output_dir=output_dir,
                ref_commit=ref_commit,  # Delta mode: ref_commit is vulnerable
            )

            # Extract and verify tarball contains VULNERABLE code (ref_commit)
            extract_dir = work_dir / "extract"
            extract_dir.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(tarball_path)],
                cwd=extract_dir,
                check=True,
            )

            code_file = extract_dir / "test-source" / "code.c"
            assert code_file.exists()
            content = code_file.read_text()

            # Should have vulnerable code from ref_commit, NOT safe code from base
            assert "BUG INTRODUCED" in content
            assert "SAFE CODE" not in content

            # ref.diff should be generated for delta mode
            assert ref_diff_path is not None
            assert ref_diff_path.exists()

    @pytest.mark.skipif(
        not shutil.which("git"),
        reason="git not available",
    )
    def test_delta_mode_patch_applies_to_tarball(self):
        """Test that CPV patches can be applied to delta mode tarball.

        This simulates the real-world scenario: patches fix the vulnerability
        in ref_commit, so they must apply cleanly to the tarball.
        """
        import os

        from crsbench.benchmark.packaging.tarball import create_source_tarball

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

            # Create repo with vulnerability
            repo_dir = work_dir / "repo"
            repo_dir.mkdir()
            run_git(["init"], cwd=repo_dir)

            # base_commit: safe code
            (repo_dir / "code.c").write_text(
                "int process(char *input) {\n"
                "    // Safe version with bounds check\n"
                "    if (strlen(input) > MAX_LEN) return -1;\n"
                "    return 0;\n"
                "}\n"
            )
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(["commit", "-m", "Safe code"], cwd=repo_dir)
            base_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # ref_commit: vulnerable code (removed bounds check)
            (repo_dir / "code.c").write_text(
                "int process(char *input) {\n"
                "    // Vulnerable: no bounds check\n"
                "    return 0;\n"
                "}\n"
            )
            run_git(["add", "-A"], cwd=repo_dir)
            run_git(
                ["commit", "-m", "Remove bounds check (vulnerability)"], cwd=repo_dir
            )
            ref_commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

            # Create a patch that fixes the vulnerability (adds back bounds check)
            fix_patch = work_dir / "fix.patch"
            fix_patch.write_text(
                "--- a/code.c\n"
                "+++ b/code.c\n"
                "@@ -1,4 +1,5 @@\n"
                " int process(char *input) {\n"
                "-    // Vulnerable: no bounds check\n"
                "+    // Fixed: bounds check restored\n"
                "+    if (strlen(input) > MAX_LEN) return -1;\n"
                "     return 0;\n"
                " }\n"
            )

            # Create tarball at ref_commit (vulnerable)
            output_dir = work_dir / "output"
            output_dir.mkdir()

            tarball_path, _ = create_source_tarball(
                repo_url=str(repo_dir),
                base_commit=base_commit,
                source_name="test-source",
                output_dir=output_dir,
                ref_commit=ref_commit,
            )

            # Extract tarball
            extract_dir = work_dir / "extract"
            extract_dir.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(tarball_path)],
                cwd=extract_dir,
                check=True,
            )

            source_dir = extract_dir / "test-source"

            # Tarball already has git initialized by _fresh_git_init()
            # Just apply the patch directly - no need to reinitialize

            # Apply the fix patch - this should succeed
            result = subprocess.run(
                ["git", "apply", str(fix_patch)],
                cwd=source_dir,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"Patch failed: {result.stderr}"

            # Verify patch was applied
            patched_content = (source_dir / "code.c").read_text()
            assert "bounds check restored" in patched_content
            assert "strlen(input) > MAX_LEN" in patched_content
