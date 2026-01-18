"""Tests for crsbench.benchmark.runtime module."""

import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.benchmark.runtime.loader import (
    has_bundled_source,
    load_benchmark_source,
    prepare_source_from_bundle,
)
from crsbench.benchmark.runtime.models import BenchmarkSource


class TestBenchmarkSource:
    """Tests for BenchmarkSource dataclass."""

    def test_bundled_source(self) -> None:
        """Test bundled source has path=None."""
        source = BenchmarkSource(path=None, is_bundled=True)
        assert source.path is None
        assert source.is_bundled is True
        assert source.requires_source_path is False

    def test_cloned_source(self, tmp_path: Path) -> None:
        """Test cloned source has path set."""
        source = BenchmarkSource(path=tmp_path, is_bundled=False)
        assert source.path == tmp_path
        assert source.is_bundled is False
        assert source.requires_source_path is True

    def test_invalid_bundled_with_path_raises(self, tmp_path: Path) -> None:
        """Test that bundled=True with path raises error."""
        with pytest.raises(ValueError, match="Bundled source should have path=None"):
            BenchmarkSource(path=tmp_path, is_bundled=True)

    def test_requires_source_path_false_when_no_path(self) -> None:
        """Test requires_source_path is False when path is None."""
        source = BenchmarkSource(path=None, is_bundled=False)
        assert source.requires_source_path is False


class TestHasBundledSource:
    """Tests for has_bundled_source function."""

    def test_no_pkgs_dir(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ doesn't exist."""
        assert has_bundled_source(tmp_path) is False

    def test_empty_pkgs_dir(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ is empty."""
        (tmp_path / "pkgs").mkdir()
        assert has_bundled_source(tmp_path) is False

    def test_pkgs_with_non_tarball(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ has no tarballs."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "readme.txt").write_text("info")
        assert has_bundled_source(tmp_path) is False

    def test_pkgs_with_tarball(self, tmp_path: Path) -> None:
        """Test returns True when pkgs/ has tarball."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source.tar.gz").write_text("dummy tarball")
        assert has_bundled_source(tmp_path) is True

    def test_pkgs_with_multiple_tarballs(self, tmp_path: Path) -> None:
        """Test returns True when pkgs/ has multiple tarballs."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source1.tar.gz").write_text("dummy")
        (pkgs / "source2.tar.gz").write_text("dummy")
        assert has_bundled_source(tmp_path) is True


class TestLoadBenchmarkSource:
    """Tests for load_benchmark_source function."""

    def test_bundled_source_returns_none_path(self, tmp_path: Path) -> None:
        """Test that bundled source returns BenchmarkSource with path=None."""
        # Create pkgs/ with tarball
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source.tar.gz").write_text("dummy")

        source = load_benchmark_source(tmp_path)

        assert source.is_bundled is True
        assert source.path is None
        assert source.requires_source_path is False

    def test_no_bundled_no_dest_raises(self, tmp_path: Path) -> None:
        """Test that missing pkgs/ and no dest_dir raises error."""
        with pytest.raises(RuntimeError, match="no dest_dir provided"):
            load_benchmark_source(tmp_path)

    def test_cloned_source_calls_repo_manager(self, tmp_path: Path) -> None:
        """Test that non-bundled source calls ensure_project_repository."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir)

            source = load_benchmark_source(tmp_path, dest_dir=dest_dir)

            mock_ensure.assert_called_once()
            assert source.is_bundled is False
            assert source.path == dest_dir

    def test_clone_failure_raises(self, tmp_path: Path) -> None:
        """Test that clone failure raises RuntimeError."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = None  # Simulate failure

            with pytest.raises(RuntimeError, match="Failed to obtain source code"):
                load_benchmark_source(tmp_path, dest_dir=dest_dir)

    def test_passes_mode_and_verbose(self, tmp_path: Path) -> None:
        """Test that mode and verbose are passed to repo_manager."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir)

            load_benchmark_source(
                tmp_path,
                dest_dir=dest_dir,
                mode="delta",
                verbose=True,
            )

            mock_ensure.assert_called_once_with(
                benchmark_dir=str(tmp_path),
                project_dir=str(dest_dir),
                mode="delta",
                verbose=True,
            )


class TestPrepareSourceFromBundleGitHistory:
    """Tests verifying git history is sanitized in pkgs mode.

    These tests ensure that CRS cannot access original git history
    in both full and delta mode when using bundled source (pkgs/).
    """

    @pytest.fixture
    def mock_benchmark(self, tmp_path: Path) -> Path:
        """Create a mock benchmark with pkgs/ tarball and ref.diff.

        The tarball simulates what bundle_benchmark creates:
        - Source code with fresh git init (single commit)
        - No original repository history
        """
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        # Create .aixcc directory with ref.diff
        aixcc_dir = benchmark_path / ".aixcc"
        aixcc_dir.mkdir()

        # Create a simple ref.diff that adds a file
        ref_diff_content = """\
diff --git a/vulnerable.c b/vulnerable.c
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/vulnerable.c
@@ -0,0 +1,5 @@
+// This file introduces a vulnerability
+void vulnerable_function() {
+    char buffer[10];
+    strcpy(buffer, user_input);  // Buffer overflow!
+}
"""
        (aixcc_dir / "ref.diff").write_text(ref_diff_content)

        # Create pkgs/ directory
        pkgs_dir = benchmark_path / "pkgs"
        pkgs_dir.mkdir()

        # Create source directory to be tarred
        source_name = "testsrc"
        source_dir = tmp_path / "source_staging" / source_name
        source_dir.mkdir(parents=True)

        # Add some source files
        (source_dir / "main.c").write_text("int main() { return 0; }\n")
        (source_dir / "README.md").write_text("Test project\n")

        # Initialize git with a single commit (simulating fresh_git_init)
        subprocess.run(["git", "init"], cwd=source_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "add", "."], cwd=source_dir, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial source"],
            cwd=source_dir,
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "CRSBench",
                "GIT_AUTHOR_EMAIL": "crsbench@example.com",
                "GIT_COMMITTER_NAME": "CRSBench",
                "GIT_COMMITTER_EMAIL": "crsbench@example.com",
            },
        )

        # Create tarball
        tarball_path = pkgs_dir / f"{source_name}.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(source_dir, arcname=source_name)

        return benchmark_path

    def _get_git_log(self, repo_path: Path) -> list[str]:
        """Get list of commit messages from git log."""
        result = subprocess.run(
            ["git", "log", "--oneline", "--format=%s"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return [
            line.strip() for line in result.stdout.strip().split("\n") if line.strip()
        ]

    def _get_git_commit_count(self, repo_path: Path) -> int:
        """Get total number of commits in repository."""
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())

    def _get_git_commit_hashes(self, repo_path: Path) -> list[str]:
        """Get list of commit hashes."""
        result = subprocess.run(
            ["git", "log", "--format=%H"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return [
            line.strip() for line in result.stdout.strip().split("\n") if line.strip()
        ]

    def test_without_ref_diff_has_single_commit(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test deltabase variant: no ref.diff applied, single commit only.

        This simulates the 'deltabase' variant where we use the base commit
        without applying ref.diff. The CRS should only see one commit.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=False,
        )

        assert source_path is not None
        assert source_path.exists()

        # Verify git history
        commit_count = self._get_git_commit_count(source_path)
        commit_messages = self._get_git_log(source_path)

        assert commit_count == 1, f"Expected 1 commit, got {commit_count}"
        assert len(commit_messages) == 1
        assert "Initial" in commit_messages[0], (
            f"Expected 'Initial' commit, got: {commit_messages}"
        )

    def test_full_mode_with_ref_diff_has_single_commit(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test full mode: ref.diff applied with squash (default), only ONE commit.

        This is critical for full mode security: after applying ref.diff,
        we re-initialize git to squash history. The CRS should only see
        one commit with the final state - no way to `git diff` to discover
        what ref.diff changed.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
            squash_history=True,  # default, explicit for clarity
        )

        assert source_path is not None
        assert source_path.exists()

        # Verify git history - must be exactly 1 commit (squashed)
        commit_count = self._get_git_commit_count(source_path)
        commit_messages = self._get_git_log(source_path)

        assert commit_count == 1, f"Expected 1 commit (squashed), got {commit_count}"
        assert len(commit_messages) == 1

        # The vulnerable file should exist (ref.diff was applied before squash)
        assert (source_path / "vulnerable.c").exists(), (
            "ref.diff should have been applied before squash"
        )

    def test_delta_mode_with_ref_diff_has_two_commits(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test delta mode: ref.diff applied without squash, TWO commits.

        In delta mode, CRS already receives ref.diff as a hint, so having
        2 commits is natural and transparent. The CRS can see what changed.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
            squash_history=False,  # delta mode - keep 2 commits
        )

        assert source_path is not None
        assert source_path.exists()

        # Verify git history - should have 2 commits
        commit_count = self._get_git_commit_count(source_path)
        commit_messages = self._get_git_log(source_path)

        assert commit_count == 2, f"Expected 2 commits (delta mode), got {commit_count}"
        assert len(commit_messages) == 2

        # Most recent commit should be "Apply ref.diff"
        assert "ref.diff" in commit_messages[0].lower(), (
            f"Expected 'ref.diff' commit, got: {commit_messages[0]}"
        )

        # The vulnerable file should exist
        assert (source_path / "vulnerable.c").exists()

    def test_no_original_commit_hashes_visible(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that original repository commit hashes are not accessible.

        The CRS should not be able to see any commit hashes from the
        original repository - only a single synthetic commit from CRSBench.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
        )

        assert source_path is not None

        # Get all commit hashes
        hashes = self._get_git_commit_hashes(source_path)

        # Only 1 synthetic commit hash should exist (squashed)
        assert len(hashes) == 1, f"Expected 1 commit hash (squashed), got {len(hashes)}"

    def test_cannot_diff_to_find_vulnerabilities(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that git diff cannot reveal what ref.diff changed.

        With only 1 commit after squashing, there's nothing to diff.
        The CRS cannot use `git diff HEAD~1` or similar to discover
        what the ref.diff contained.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
        )

        assert source_path is not None

        # Only 1 commit exists - nothing to diff against
        hashes = self._get_git_commit_hashes(source_path)
        assert len(hashes) == 1, "Should have exactly 1 commit (squashed)"

        # Verify that `git diff HEAD~1` would fail (no parent commit)
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=source_path,
            capture_output=True,
            text=True,
        )
        # This should fail because there's no parent commit
        assert result.returncode != 0, "HEAD~1 should not exist with single commit"

    def test_git_log_shows_crsbench_author(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that commits show CRSBench as author, not original authors.

        This ensures no information leaks through commit metadata.
        """
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
        )

        assert source_path is not None

        # Get author info
        result = subprocess.run(
            ["git", "log", "--format=%an <%ae>"],
            cwd=source_path,
            capture_output=True,
            text=True,
            check=True,
        )
        authors = [
            line.strip() for line in result.stdout.strip().split("\n") if line.strip()
        ]

        # All authors should be CRSBench
        for author in authors:
            assert "CRSBench" in author or "crsbench" in author.lower(), (
                f"Expected CRSBench author, got: {author}"
            )

    def test_ref_diff_creates_vulnerable_file(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that ref.diff actually applies and creates the vulnerable code."""
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
            apply_ref_diff=True,
        )

        assert source_path is not None

        # The vulnerable.c file should exist (added by ref.diff)
        vulnerable_file = source_path / "vulnerable.c"
        assert vulnerable_file.exists(), "ref.diff should have created vulnerable.c"

        content = vulnerable_file.read_text()
        assert "vulnerable_function" in content
        assert "strcpy" in content  # The vulnerability
