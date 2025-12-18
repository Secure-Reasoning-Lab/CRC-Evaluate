"""Tests for repository manager."""

import os
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest import mock

from crsbench.utils.repo_manager import (
    get_repo_info_from_benchmark,
    derive_repo_name_from_url,
    clone_repository,
    ensure_project_repository,
    find_or_clone_project,
    set_gitcache
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

        assert info["repo_url"] == "git@github.com:Team-Atlanta/cp-c-curl.git"
        assert info["base_commit"] == "abc123def456"
        assert info["ref_commit"] == "def789ghi012"

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

        assert info["base_commit"] == "xyz789abc123"
        assert info["ref_commit"] is None


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

    @mock.patch('subprocess.run')
    def test_clone_already_exists(self, mock_run, tmp_path):
        """Test when directory already exists and is a git repo."""
        repo_dir = tmp_path / "existing-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        # Mock successful reset
        mock_run.return_value = mock.Mock(returncode=0, stderr="")

        result = clone_repository(
            "https://example.com/repo.git",
            str(repo_dir),
            verbose=False
        )

        assert result is True
        # Verify git reset --hard was called
        assert mock_run.called
        reset_call = mock_run.call_args_list[0]
        assert "reset" in str(reset_call)
        assert "--hard" in str(reset_call)

    @mock.patch('subprocess.run')
    def test_clone_already_exists_reset_failure(self, mock_run, tmp_path):
        """Test when directory exists but git reset fails - should still succeed."""
        repo_dir = tmp_path / "existing-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        # Mock failed reset
        mock_run.return_value = mock.Mock(returncode=1, stderr="error resetting")

        result = clone_repository(
            "https://example.com/repo.git",
            str(repo_dir),
            verbose=False
        )

        # Should still return True even if reset fails
        assert result is True
        assert mock_run.called

    def test_clone_exists_but_not_git(self, tmp_path):
        """Test when directory exists but is not a git repo."""
        repo_dir = tmp_path / "not-git"
        repo_dir.mkdir()

        result = clone_repository(
            "https://example.com/repo.git",
            str(repo_dir),
            verbose=False
        )

        assert result is False

    @mock.patch('subprocess.run')
    def test_clone_success(self, mock_run, tmp_path):
        """Test successful git clone."""
        mock_run.return_value = mock.Mock(returncode=0, stderr="")

        repo_dir = tmp_path / "new-repo"

        result = clone_repository(
            "https://example.com/repo.git",
            str(repo_dir),
            verbose=False
        )

        assert result is True
        # Verify git clone was called
        assert mock_run.call_count >= 1
        assert "clone" in str(mock_run.call_args_list[0])

    @mock.patch('subprocess.run')
    def test_clone_failure(self, mock_run, tmp_path):
        """Test failed git clone."""
        mock_run.return_value = mock.Mock(returncode=1, stderr="fatal: repository not found")

        repo_dir = tmp_path / "failed-repo"

        result = clone_repository(
            "https://example.com/invalid.git",
            str(repo_dir),
            verbose=False
        )

        assert result is False


# ============================================================================
# Test ensure_project_repository
# ============================================================================

class TestEnsureProjectRepository:
    """Test ensure_project_repository function."""

    @mock.patch('subprocess.run')
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
            verbose=False
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
        with mock.patch('crsbench.utils.repo_manager.clone_repository') as mock_clone:
            mock_clone.return_value = True

            result = ensure_project_repository(
                benchmark_dir=str(temp_benchmark_dir),
                repos_dir=str(repos_dir),
                verbose=False
            )

            # Should derive name from URL with commit: cp-c-curl-abc123de
            assert result == str(repos_dir / "cp-c-curl-abc123de")
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
            benchmark_name="nonexistent",
            benchmarks_root=str(tmp_path),
            verbose=False
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
            verbose=False
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
        with mock.patch('shutil.which', return_value='/usr/bin/gitcache'):
            # Should not raise any exception
            set_gitcache(True)
            # Cleanup
            set_gitcache(False)

    def test_enable_gitcache_when_not_installed(self):
        """Test enabling gitcache when it's not installed."""
        with mock.patch('shutil.which', return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                set_gitcache(True)

            error_msg = str(exc_info.value)
            assert "gitcache is not installed" in error_msg
            assert "github.com/seeraven/gitcache" in error_msg


# ============================================================================
# Marker for running specific test groups
# ============================================================================

# Run tests:
#   uv run pytest tests/test_repo_manager.py -v
