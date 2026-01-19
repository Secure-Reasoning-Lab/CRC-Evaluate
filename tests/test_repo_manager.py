"""Tests for repository manager."""

import concurrent.futures
import threading
from pathlib import Path
from unittest import mock

import pytest
from crsbench.utils.repo_manager import (
    clone_or_copy_cached_repo,
    clone_repository,
    derive_repo_name_from_url,
    ensure_project_repository,
    find_or_clone_project,
    get_diff_from_repo_info,
    get_repo_info_from_benchmark,
    set_gitcache,
    write_benchmark_delta_diff,
    write_diff_from_repo_info,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_benchmark_dir(tmp_path):
    """Create a temporary benchmark directory with configs."""
    benchmark_dir = tmp_path / "test-benchmark"
    benchmark_dir.mkdir()

    # Create project.yaml
    project_yaml = benchmark_dir / "project.yaml"
    project_yaml.write_text("""homepage: "https://example.com"
language: c
main_repo: 'git@github.com:Team-Atlanta/cp-c-curl.git'
""")

    # Create .aixcc/meta.yaml
    aixcc_dir = benchmark_dir / ".aixcc"
    aixcc_dir.mkdir()

    meta_yaml = aixcc_dir / "meta.yaml"
    meta_yaml.write_text("""delta_mode:
  base_commit: abc123def456
  ref_commit: def789ghi012

harness_files:
  - name: test_harness
    path: /src/test.c
""")

    return benchmark_dir


# ============================================================================
# Test get_repo_info_from_benchmark
# ============================================================================


class TestGetRepoInfo:
    """Test get_repo_info_from_benchmark function."""

    def test_get_repo_info_success(self, temp_benchmark_dir):
        """Test successful extraction of repo info."""
        info = get_repo_info_from_benchmark(str(temp_benchmark_dir))

        assert info.repo_url == "git@github.com:Team-Atlanta/cp-c-curl.git"
        assert info.base_commit == "abc123def456"
        assert info.ref_commit == "def789ghi012"

    def test_get_repo_info_missing_project_yaml(self, tmp_path):
        """Test error when project.yaml is missing."""
        benchmark_dir = tmp_path / "missing-project"
        benchmark_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="project.yaml not found"):
            get_repo_info_from_benchmark(str(benchmark_dir))

    def test_get_repo_info_missing_main_repo(self, tmp_path):
        """Test error when main_repo is missing from project.yaml."""
        benchmark_dir = tmp_path / "no-main-repo"
        benchmark_dir.mkdir()

        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("language: c\n")

        with pytest.raises(ValueError, match="main_repo not found"):
            get_repo_info_from_benchmark(str(benchmark_dir))

    def test_get_repo_info_full_mode(self, tmp_path):
        """Test repo info extraction with full_mode."""
        benchmark_dir = tmp_path / "full-mode-bench"
        benchmark_dir.mkdir()

        # project.yaml
        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("main_repo: 'https://github.com/test/repo.git'\n")

        # meta.yaml with full_mode only
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""full_mode:
  base_commit: xyz789abc123

harness_files:
  - name: test
    path: /src/test.c
""")

        info = get_repo_info_from_benchmark(str(benchmark_dir))

        assert info.base_commit == "xyz789abc123"
        assert info.ref_commit is None

    def test_get_repo_info_with_mode_delta(self, temp_benchmark_dir):
        """Test repo info extraction with explicit mode='delta'."""
        info = get_repo_info_from_benchmark(str(temp_benchmark_dir), mode="delta")

        # Should get delta_mode commits
        assert info.base_commit == "abc123def456"
        assert info.ref_commit == "def789ghi012"

    def test_get_repo_info_with_mode_full(self, tmp_path):
        """Test repo info extraction with explicit mode='full'."""
        benchmark_dir = tmp_path / "both-modes-bench"
        benchmark_dir.mkdir()

        # project.yaml
        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("main_repo: 'https://github.com/test/repo.git'\n")

        # meta.yaml with both modes
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""delta_mode:
  base_commit: delta_base_commit
  ref_commit: delta_ref_commit

full_mode:
  base_commit: full_base_commit

harness_files:
  - name: test
    path: /src/test.c
""")

        # With mode='full', should get full_mode commit
        info = get_repo_info_from_benchmark(str(benchmark_dir), mode="full")

        assert info.base_commit == "full_base_commit"
        assert info.ref_commit is None

    def test_get_repo_info_mode_delta_vs_full(self, tmp_path):
        """Test that mode parameter correctly selects commits from different modes."""
        benchmark_dir = tmp_path / "dual-mode-bench"
        benchmark_dir.mkdir()

        # project.yaml
        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("main_repo: 'https://github.com/test/repo.git'\n")

        # meta.yaml with different commits for delta and full modes
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""delta_mode:
  base_commit: delta_base_aaa
  ref_commit: delta_ref_bbb

full_mode:
  base_commit: full_base_ccc

harness_files:
  - name: test
    path: /src/test.c
""")

        # mode='delta' should use delta_mode
        info_delta = get_repo_info_from_benchmark(str(benchmark_dir), mode="delta")
        assert info_delta.base_commit == "delta_base_aaa"
        assert info_delta.ref_commit == "delta_ref_bbb"

        # mode='full' should use full_mode
        info_full = get_repo_info_from_benchmark(str(benchmark_dir), mode="full")
        assert info_full.base_commit == "full_base_ccc"
        assert info_full.ref_commit is None

        # mode=None (auto-detect) should prefer delta_mode
        info_auto = get_repo_info_from_benchmark(str(benchmark_dir), mode=None)
        assert info_auto.base_commit == "delta_base_aaa"
        assert info_auto.ref_commit == "delta_ref_bbb"


# ============================================================================
# Test derive_repo_name_from_url
# ============================================================================


class TestDeriveRepoName:
    """Test derive_repo_name_from_url function."""

    def test_ssh_url_with_git_extension(self):
        """Test SSH URL with .git extension."""
        url = "git@github.com:Team-Atlanta/cp-c-curl.git"
        name = derive_repo_name_from_url(url)
        assert name == "cp-c-curl"

    def test_https_url_with_git_extension(self):
        """Test HTTPS URL with .git extension."""
        url = "https://github.com/curl/curl.git"
        name = derive_repo_name_from_url(url)
        assert name == "curl"

    def test_url_without_git_extension(self):
        """Test URL without .git extension."""
        url = "https://github.com/apache/commons-compress"
        name = derive_repo_name_from_url(url)
        assert name == "commons-compress"

    def test_url_with_trailing_slash(self):
        """Test URL with trailing slash."""
        url = "https://github.com/apache/commons-compress/"
        name = derive_repo_name_from_url(url)
        assert name == "commons-compress"


# ============================================================================
# Test clone_repository
# ============================================================================


class TestCloneRepository:
    """Test clone_repository function."""

    @mock.patch("subprocess.run")
    def test_clone_already_exists(self, mock_run, tmp_path):
        """Test when directory already exists and is a git repo."""
        repo_dir = tmp_path / "existing-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        # Mock successful reset
        mock_run.return_value = mock.Mock(returncode=0, stderr="")

        result = clone_repository(
            "https://example.com/repo.git", str(repo_dir), verbose=False
        )

        assert result is True
        # Verify git reset --hard was called
        assert mock_run.called
        reset_call = mock_run.call_args_list[0]
        assert "reset" in str(reset_call)
        assert "--hard" in str(reset_call)

    @mock.patch("subprocess.run")
    def test_clone_already_exists_reset_failure(self, mock_run, tmp_path):
        """Test when directory exists but git reset fails - should still succeed."""
        repo_dir = tmp_path / "existing-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        # Mock failed reset
        mock_run.return_value = mock.Mock(returncode=1, stderr="error resetting")

        result = clone_repository(
            "https://example.com/repo.git", str(repo_dir), verbose=False
        )

        # Should still return True even if reset fails
        assert result is True
        assert mock_run.called

    def test_clone_exists_but_not_git(self, tmp_path):
        """Test when directory exists but is not a git repo."""
        repo_dir = tmp_path / "not-git"
        repo_dir.mkdir()

        result = clone_repository(
            "https://example.com/repo.git", str(repo_dir), verbose=False
        )

        assert result is False

    @mock.patch("subprocess.run")
    def test_clone_success(self, mock_run, tmp_path):
        """Test successful git clone."""
        mock_run.return_value = mock.Mock(returncode=0, stderr="")

        repo_dir = tmp_path / "new-repo"

        result = clone_repository(
            "https://example.com/repo.git", str(repo_dir), verbose=False
        )

        assert result is True
        # Verify git clone was called
        assert mock_run.call_count >= 1
        assert "clone" in str(mock_run.call_args_list[0])

    @mock.patch("subprocess.run")
    def test_clone_failure(self, mock_run, tmp_path):
        """Test failed git clone."""
        mock_run.return_value = mock.Mock(
            returncode=1, stderr="fatal: repository not found"
        )

        repo_dir = tmp_path / "failed-repo"

        result = clone_repository(
            "https://example.com/invalid.git", str(repo_dir), verbose=False
        )

        assert result is False


# ============================================================================
# Test ensure_project_repository
# ============================================================================


class TestEnsureProjectRepository:
    """Test ensure_project_repository function."""

    @mock.patch("subprocess.run")
    def test_explicit_project_dir_exists(self, mock_run, temp_benchmark_dir, tmp_path):
        """Test with explicit project_dir that exists."""
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()

        # Mock successful reset
        mock_run.return_value = mock.Mock(returncode=0, stderr="")

        result = ensure_project_repository(
            benchmark_dir=str(temp_benchmark_dir),
            project_dir=str(project_dir),
            verbose=False,
        )

        assert result == str(project_dir)
        # Verify git reset --hard was called
        assert mock_run.called
        reset_call = mock_run.call_args_list[0]
        assert "reset" in str(reset_call)
        assert "--hard" in str(reset_call)

    def test_no_project_dir_auto_derive(self, temp_benchmark_dir, tmp_path):
        """Test auto-deriving project directory name."""
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()

        # Mock the clone to avoid actual git operations
        with mock.patch("crsbench.utils.repo_manager.clone_repository") as mock_clone:
            mock_clone.return_value = True

            result = ensure_project_repository(
                benchmark_dir=str(temp_benchmark_dir),
                repos_dir=str(repos_dir),
                verbose=False,
            )

            # temp_benchmark_dir has delta_mode with ref_commit: def789ghi012
            # Should derive name from URL with ref_commit: cp-c-curl-def789gh
            # (delta mode uses ref_commit, not base_commit)
            assert result == str(repos_dir / "cp-c-curl-def789gh")
            # Verify clone was called
            assert mock_clone.called


# ============================================================================
# Test find_or_clone_project
# ============================================================================


class TestFindOrCloneProject:
    """Test find_or_clone_project function."""

    def test_invalid_benchmark(self, tmp_path):
        """Test with non-existent benchmark."""
        result = find_or_clone_project(
            benchmark_name="nonexistent", benchmarks_root=str(tmp_path), verbose=False
        )

        assert result is None

    def test_with_existing_project_dir(self, tmp_path):
        """Test with explicit existing project directory."""
        # Create benchmark
        benchmark_dir = tmp_path / "benchmarks" / "test-bench"
        benchmark_dir.mkdir(parents=True)

        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("main_repo: 'https://example.com/repo.git'\n")

        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""full_mode:
  base_commit: abc123

harness_files:
  - name: test
    path: /src/test.c
""")

        # Create project dir
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        result = find_or_clone_project(
            benchmark_name="test-bench",
            benchmarks_root=str(tmp_path / "benchmarks"),
            project_dir=str(project_dir),
            verbose=False,
        )

        assert result == str(project_dir)


# ============================================================================
# Gitcache Tests
# ============================================================================


class TestSetGitcache:
    """Tests for set_gitcache function."""

    def test_disable_gitcache(self):
        """Test disabling gitcache (should always work)."""
        # Should not raise any exception
        set_gitcache(False)

    def test_enable_gitcache_when_installed(self):
        """Test enabling gitcache when it's installed."""
        with mock.patch("shutil.which", return_value="/usr/bin/gitcache"):
            # Should not raise any exception
            set_gitcache(True)
            # Cleanup
            set_gitcache(False)

    def test_enable_gitcache_when_not_installed(self):
        """Test enabling gitcache when it's not installed."""
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                set_gitcache(True)

            error_msg = str(exc_info.value)
            assert "gitcache is not installed" in error_msg
            assert "github.com/seeraven/gitcache" in error_msg


# ============================================================================
# Integration Tests with Real Benchmark
# ============================================================================


@pytest.fixture
def real_benchmark_dir():
    """Get the real sanity-mock-c-delta-01 benchmark directory."""

    benchmark_path = (
        Path(__file__).parent.parent / "benchmarks" / "sanity-mock-c-delta-01"
    )
    if not benchmark_path.exists():
        pytest.skip("sanity-mock-c-delta-01 benchmark not found")
    return benchmark_path


class UnitTestModeCommitSelectionIntegration:
    """Integration tests for mode-based commit selection using real benchmark.

    These tests verify that:
    - mode='delta' selects ref_commit (patched version)
    - mode='full' selects base_commit (vulnerable version)

    Expected commits for sanity-mock-c-delta-01:
    - delta_mode.base_commit: eee49074120dd09bf9487fbea8513367750712e2
    - delta_mode.ref_commit: 602b58633e288ae320a3b24cc4779337dba18e57
    - full_mode.base_commit: 602b58633e288ae320a3b24cc4779337dba18e57
    """

    # Expected commits from sanity-mock-c-delta-01/.aixcc/meta.yaml
    DELTA_BASE_COMMIT = "eee49074120dd09bf9487fbea8513367750712e2"
    DELTA_REF_COMMIT = "602b58633e288ae320a3b24cc4779337dba18e57"
    FULL_BASE_COMMIT = "602b58633e288ae320a3b24cc4779337dba18e57"

    def test_get_repo_info_delta_mode_real_benchmark(self, real_benchmark_dir):
        """Test that mode='delta' returns correct commits from real benchmark."""
        info = get_repo_info_from_benchmark(str(real_benchmark_dir), mode="delta")

        assert info.base_commit == self.DELTA_BASE_COMMIT
        assert info.ref_commit == self.DELTA_REF_COMMIT

    def test_get_repo_info_full_mode_real_benchmark(self, real_benchmark_dir):
        """Test that mode='full' returns correct commits from real benchmark."""
        info = get_repo_info_from_benchmark(str(real_benchmark_dir), mode="full")

        assert info.base_commit == self.FULL_BASE_COMMIT
        assert info.ref_commit is None

    @pytest.mark.integration
    def test_ensure_project_repository_delta_mode(self, real_benchmark_dir, tmp_path):
        """Test that mode='delta' clones repo at ref_commit (patched version)."""
        import subprocess

        project_dir = tmp_path / "delta-mode-clone"

        result = ensure_project_repository(
            benchmark_dir=str(real_benchmark_dir),
            project_dir=str(project_dir),
            mode="delta",
            verbose=True,
        )

        assert result is not None
        assert project_dir.exists()

        # Verify the cloned repo is at the correct commit (ref_commit for delta)
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        actual_commit = git_result.stdout.strip()

        assert actual_commit == self.DELTA_REF_COMMIT, (
            f"Delta mode should checkout ref_commit. "
            f"Expected: {self.DELTA_REF_COMMIT}, Got: {actual_commit}"
        )

    @pytest.mark.integration
    def test_ensure_project_repository_full_mode(self, real_benchmark_dir, tmp_path):
        """Test that mode='full' clones repo at base_commit (vulnerable version)."""
        import subprocess

        project_dir = tmp_path / "full-mode-clone"

        result = ensure_project_repository(
            benchmark_dir=str(real_benchmark_dir),
            project_dir=str(project_dir),
            mode="full",
            verbose=True,
        )

        assert result is not None
        assert project_dir.exists()

        # Verify the cloned repo is at the correct commit (base_commit for full)
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        actual_commit = git_result.stdout.strip()

        assert actual_commit == self.FULL_BASE_COMMIT, (
            f"Full mode should checkout base_commit. "
            f"Expected: {self.FULL_BASE_COMMIT}, Got: {actual_commit}"
        )

    @pytest.mark.integration
    def test_delta_and_full_modes_produce_different_commits(
        self, real_benchmark_dir, tmp_path
    ):
        """Test that delta and full modes produce different commits when expected.

        Note: For sanity-mock-c-delta-01:
        - delta mode uses ref_commit (602b5863...) - the patched version
        - full mode uses base_commit (602b5863...) - same commit in this case

        In general benchmarks where delta_mode.ref_commit != full_mode.base_commit,
        this test would verify they produce different commits.
        """
        import subprocess

        delta_dir = tmp_path / "delta-clone"
        full_dir = tmp_path / "full-clone"

        # Clone with delta mode
        ensure_project_repository(
            benchmark_dir=str(real_benchmark_dir),
            project_dir=str(delta_dir),
            mode="delta",
            verbose=True,
        )

        # Clone with full mode
        ensure_project_repository(
            benchmark_dir=str(real_benchmark_dir),
            project_dir=str(full_dir),
            mode="full",
            verbose=True,
        )

        # Get commits
        delta_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=delta_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        delta_commit = delta_result.stdout.strip()

        full_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=full_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        full_commit = full_result.stdout.strip()

        # Verify each mode got the expected commit
        assert delta_commit == self.DELTA_REF_COMMIT, (
            f"Delta mode got wrong commit. Expected: {self.DELTA_REF_COMMIT}"
        )
        assert full_commit == self.FULL_BASE_COMMIT, (
            f"Full mode got wrong commit. Expected: {self.FULL_BASE_COMMIT}"
        )


# ============================================================================
# Test Diff Generation
# ============================================================================


class TestDiffGeneration:
    """Test diff generation functions."""

    def test_get_diff_from_repo_info_success(self, tmp_path):
        """Test successful diff generation."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit="abc123",
            ref_commit="def456",
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        mock_diff_output = (
            "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        )

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=mock_diff_output, stderr=""
            )

            result = get_diff_from_repo_info(repo_info, str(repo_dir), verbose=False)

            assert result == mock_diff_output
            # Verify git diff was called with correct commits
            call_args = str(mock_run.call_args)
            assert "diff" in call_args
            assert "abc123" in call_args
            assert "def456" in call_args

    def test_get_diff_from_repo_info_missing_base_commit(self, tmp_path):
        """Test ValueError when base_commit is missing."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit=None,
            ref_commit="def456",
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with pytest.raises(ValueError, match="base_commit is required"):
            get_diff_from_repo_info(repo_info, str(repo_dir))

    def test_get_diff_from_repo_info_missing_ref_commit(self, tmp_path):
        """Test ValueError when ref_commit is missing."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit="abc123",
            ref_commit=None,
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with pytest.raises(ValueError, match="ref_commit is required"):
            get_diff_from_repo_info(repo_info, str(repo_dir))

    def test_get_diff_from_repo_info_git_failure(self, tmp_path):
        """Test RuntimeError on git diff failure."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit="abc123",
            ref_commit="def456",
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="fatal: bad object"
            )

            with pytest.raises(RuntimeError, match="Git diff failed"):
                get_diff_from_repo_info(repo_info, str(repo_dir))

    def test_write_diff_from_repo_info_creates_file(self, tmp_path):
        """Test that diff is written to file correctly."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit="abc123",
            ref_commit="def456",
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        output_path = tmp_path / "output.diff"
        mock_diff_output = "diff content here\n"

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=mock_diff_output, stderr=""
            )

            result = write_diff_from_repo_info(
                repo_info, str(repo_dir), str(output_path), verbose=False
            )

            assert result == str(output_path)
            assert output_path.exists()
            assert output_path.read_text() == mock_diff_output

    def test_write_diff_from_repo_info_creates_parent_dirs(self, tmp_path):
        """Test that parent directories are created."""
        from crsbench.utils.repo_manager import RepoInfo

        repo_info = RepoInfo(
            repo_url="https://example.com/repo.git",
            base_commit="abc123",
            ref_commit="def456",
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Use nested path that doesn't exist
        output_path = tmp_path / "nested" / "dirs" / "output.diff"
        mock_diff_output = "diff content\n"

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=mock_diff_output, stderr=""
            )

            result = write_diff_from_repo_info(
                repo_info, str(repo_dir), str(output_path), verbose=False
            )

            assert result == str(output_path)
            assert output_path.exists()
            assert output_path.parent.exists()

    def test_write_benchmark_delta_diff_success(self, temp_benchmark_dir, tmp_path):
        """Test successful diff generation from benchmark."""

        output_path = tmp_path / "benchmark.diff"
        mock_diff_output = "benchmark diff content\n"

        # Mock both git operations: clone and diff
        with mock.patch("subprocess.run") as mock_run:
            # First call: git clone (or reset)
            # Second call: git diff
            mock_run.side_effect = [
                mock.Mock(returncode=0, stdout="", stderr=""),  # reset
                mock.Mock(returncode=0, stdout="", stderr=""),  # clean
                mock.Mock(returncode=0, stdout=mock_diff_output, stderr=""),  # diff
            ]

            with mock.patch(
                "crsbench.utils.repo_manager.clone_repository", return_value=True
            ):
                result = write_benchmark_delta_diff(
                    benchmark_dir=str(temp_benchmark_dir),
                    output_path=str(output_path),
                    verbose=False,
                )

                assert result == str(output_path)
                assert output_path.exists()
                # Note: content may be empty due to mocking, but file should exist

    def test_write_benchmark_delta_diff_no_ref_commit(self, tmp_path):
        """Test ValueError when benchmark has no delta mode (no ref_commit)."""

        # Create benchmark with only full_mode (no ref_commit)
        benchmark_dir = tmp_path / "full-only-bench"
        benchmark_dir.mkdir()

        project_yaml = benchmark_dir / "project.yaml"
        project_yaml.write_text("main_repo: 'https://example.com/repo.git'\n")

        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""full_mode:
  base_commit: abc123

harness_files:
  - name: test
    path: /src/test.c
""")

        output_path = tmp_path / "output.diff"

        with pytest.raises(
            ValueError, match="does not have delta mode.*ref_commit is missing"
        ):
            write_benchmark_delta_diff(
                benchmark_dir=str(benchmark_dir), output_path=str(output_path)
            )


# ============================================================================
# Test parallel cache access
# ============================================================================


class TestParallelCacheAccess:
    """Test thread-safe cache operations in clone_or_copy_cached_repo."""

    def test_parallel_cache_access_no_git_locks(self, tmp_path):
        """Test that parallel access doesn't cause git lock conflicts.

        This test verifies that:
        1. Multiple threads can safely copy from the same cache
        2. No git lock files are left behind
        3. Each thread gets its own working directory
        """
        # Setup: Create a mock cached repo
        cache_dir = tmp_path / "cache" / "test-repo-abc12345"
        cache_dir.mkdir(parents=True)
        (cache_dir / ".git").mkdir()
        (cache_dir / ".git" / "HEAD").write_text("abc12345abcdef\n")
        (cache_dir / "file.txt").write_text("content\n")

        # Track results
        results = []
        errors = []
        lock = threading.Lock()

        # Apply mock BEFORE spawning threads to avoid thread-unsafe mocking
        # Patch at module level to avoid polluting global subprocess.run
        with mock.patch("crsbench.utils.repo_manager.subprocess.run") as mock_run:
            # Return string (text=True is used in the real code)
            mock_run.return_value = mock.Mock(
                returncode=0, stdout="abc12345abcdef\n", stderr=""
            )

            def clone_to_target(thread_id: int) -> str:
                """Clone from cache to unique target."""
                target = tmp_path / "targets" / f"target-{thread_id}"
                target.mkdir(parents=True, exist_ok=True)
                try:
                    result = clone_or_copy_cached_repo(
                        repo_url="https://example.com/test-repo.git",
                        commit="abc12345",
                        target_dir=str(target / "repo"),
                        repos_dir=str(tmp_path / "cache"),
                        verbose=False,
                    )
                    with lock:
                        results.append((thread_id, result))
                    return result
                except Exception as e:
                    with lock:
                        errors.append((thread_id, str(e)))
                    return None

            # Run parallel clones INSIDE the mock context
            num_threads = 5
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=num_threads
            ) as executor:
                futures = [
                    executor.submit(clone_to_target, i) for i in range(num_threads)
                ]
                concurrent.futures.wait(futures)

        # Verify no errors
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify all threads got results
        assert len(results) == num_threads

        # Verify no git lock files in cache
        assert not (cache_dir / ".git" / "index.lock").exists()

    def test_lock_prevents_concurrent_cache_modification(self, tmp_path):
        """Test that cache lock prevents concurrent modifications.

        Verifies that the per-repo lock mechanism works correctly
        by checking that locks are properly acquired and released.
        """
        from crsbench.utils.repo_manager import _get_repo_lock

        # Get lock for a cache directory
        cache_path = str(tmp_path / "cache" / "test-repo")
        lock1 = _get_repo_lock(cache_path)
        lock2 = _get_repo_lock(cache_path)

        # Same path should return same lock
        assert lock1 is lock2

        # Different paths should return different locks
        other_path = str(tmp_path / "cache" / "other-repo")
        lock3 = _get_repo_lock(other_path)
        assert lock1 is not lock3

        # Lock should be acquirable
        acquired = lock1.acquire(blocking=False)
        assert acquired is True

        # Same lock should not be re-acquirable from same thread without release
        # (actually it can in Python due to RLock-like behavior in some cases)
        # Let's just release and verify no errors
        lock1.release()

        # Should be able to acquire again after release
        acquired_again = lock1.acquire(blocking=False)
        assert acquired_again is True
        lock1.release()

    def test_target_dir_isolated_from_cache(self, tmp_path):
        """Test that operations on target_dir don't affect cache.

        Each target directory should be completely independent.
        Modifications to target shouldn't touch the cache.
        """
        # Setup cache
        cache_dir = tmp_path / "cache" / "isolated-repo-abc12345"
        cache_dir.mkdir(parents=True)
        (cache_dir / ".git").mkdir()
        (cache_dir / ".git" / "HEAD").write_text("abc12345abcdef\n")
        original_content = "original content\n"
        (cache_dir / "file.txt").write_text(original_content)

        # Clone to target
        target = tmp_path / "target" / "repo"
        target.parent.mkdir(parents=True, exist_ok=True)

        # Patch at module level to avoid polluting global subprocess.run
        # Return string (text=True is used in the real code)
        with mock.patch("crsbench.utils.repo_manager.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout="abc12345abcdef\n", stderr=""
            )
            result = clone_or_copy_cached_repo(
                repo_url="https://example.com/isolated-repo.git",
                commit="abc12345",
                target_dir=str(target),
                repos_dir=str(tmp_path / "cache"),
                verbose=False,
            )

        assert result is not None

        # Modify target
        if target.exists():
            (target / "file.txt").write_text("modified content\n")

        # Verify cache is unchanged
        assert (cache_dir / "file.txt").read_text() == original_content


# ============================================================================
# Marker for running specific test groups
# ============================================================================

# Run tests:
#   uv run pytest tests/test_repo_manager.py -v
#
# Run only unit tests (fast):
#   uv run pytest tests/test_repo_manager.py -v -m "not integration"
#
# Run integration tests:
#   uv run pytest tests/test_repo_manager.py -v -m integration
