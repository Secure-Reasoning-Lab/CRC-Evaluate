"""Tests for the path_resolver module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from crsbench.evaluation.path_resolver import (
    resolve_harness_path,
    get_harness_source_path,
    RepositoryError,
    _resolve_repo_path
)
from crsbench.validation.schemas import HarnessFile


# ============================================================================
# Test Helpers
# ============================================================================

def create_test_benchmark(tmp_path: Path, project_name: str = "json-c") -> Path:
    """Create a minimal test benchmark directory structure."""
    benchmark = tmp_path / "benchmarks" / project_name
    benchmark.mkdir(parents=True)

    # Create project.yaml
    (benchmark / "project.yaml").write_text(f"""
homepage: https://github.com/{project_name}/{project_name}
language: c
main_repo: https://github.com/{project_name}/{project_name}.git
    """)

    # Create meta.yaml
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()
    (aixcc / "meta.yaml").write_text("""
full_mode:
  base_commit: abc123def456

harness_files:
  - name: test_harness
    path: $REPO/test/harness.c
    """)

    return benchmark


def create_test_repository(tmp_path: Path, project_name: str = "json-c") -> Path:
    """Create a minimal test repository with harness file."""
    repo = tmp_path / "repos" / project_name
    repo.mkdir(parents=True)

    # Create .git directory to mark as git repo
    (repo / ".git").mkdir()

    # Create test harness file
    test_dir = repo / "test"
    test_dir.mkdir()
    harness_file = test_dir / "harness.c"
    harness_file.write_text("// Test harness code\nint main() { return 0; }")

    return repo


# ============================================================================
# Test $REPO Variable Resolution
# ============================================================================

class TestRepoVariableResolution:
    """Test resolution of $REPO variable in harness paths."""

    def test_resolve_repo_variable_basic(self, tmp_path):
        """Test basic $REPO variable resolution."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        repos_dir = tmp_path / "repos"

        # Test
        resolved = resolve_harness_path(
            "$REPO/test/harness.c",
            benchmark_dir=benchmark,
            repos_dir=repos_dir
        )

        # Verify
        assert resolved == repo / "test" / "harness.c"
        assert resolved.exists()
        assert resolved.is_absolute()

    def test_resolve_repo_variable_with_project_dir_override(self, tmp_path):
        """Test $REPO resolution with explicit project_dir override."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        custom_repo = tmp_path / "custom" / "json-c"
        custom_repo.mkdir(parents=True)
        (custom_repo / "test").mkdir()
        (custom_repo / "test" / "harness.c").write_text("// custom")

        # Test with explicit project_dir (bypasses repo_manager)
        resolved = resolve_harness_path(
            "$REPO/test/harness.c",
            benchmark_dir=benchmark,
            project_dir=custom_repo
        )

        # Verify - should use custom_repo not repos_dir
        assert resolved == custom_repo / "test" / "harness.c"
        assert resolved.exists()

    def test_resolve_repo_variable_missing_file(self, tmp_path):
        """Test error when $REPO file doesn't exist."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        repos_dir = tmp_path / "repos"

        # Test
        with pytest.raises(FileNotFoundError) as exc_info:
            resolve_harness_path(
                "$REPO/test/nonexistent.c",
                benchmark_dir=benchmark,
                repos_dir=repos_dir
            )

        # Verify error message
        error_msg = str(exc_info.value)
        assert "Harness file not found" in error_msg
        assert "nonexistent.c" in error_msg
        assert "$REPO/test/nonexistent.c" in error_msg

    @patch('crsbench.evaluation.path_resolver.ensure_project_repository')
    def test_resolve_repo_variable_clone_failure(self, mock_ensure_repo, tmp_path):
        """Test error when repository cloning fails."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        mock_ensure_repo.return_value = None  # Simulate failure

        # Test
        with pytest.raises(RepositoryError) as exc_info:
            resolve_harness_path(
                "$REPO/test/harness.c",
                benchmark_dir=benchmark,
                repos_dir=tmp_path / "repos"
            )

        # Verify error message
        error_msg = str(exc_info.value)
        assert "repo_manager returned None" in error_msg or "Failed to obtain repository" in error_msg


# ============================================================================
# Test $PROJECT Variable Resolution
# ============================================================================

class TestProjectVariableResolution:
    """Test resolution of $PROJECT variable in harness paths."""

    def test_resolve_project_variable_basic(self, tmp_path):
        """Test basic $PROJECT variable resolution."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        harness_file = benchmark / "fuzz.c"
        harness_file.write_text("// Project-level harness")

        # Test
        resolved = resolve_harness_path(
            "$PROJECT/fuzz.c",
            benchmark_dir=benchmark
        )

        # Verify
        assert resolved == harness_file
        assert resolved.exists()
        assert resolved.is_absolute()

    def test_resolve_project_variable_with_project_dir_override(self, tmp_path):
        """Test $PROJECT resolution with explicit project_dir override."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        custom_project = tmp_path / "custom_project"
        custom_project.mkdir()
        (custom_project / "fuzz.c").write_text("// custom project harness")

        # Test with explicit project_dir
        resolved = resolve_harness_path(
            "$PROJECT/fuzz.c",
            benchmark_dir=benchmark,
            project_dir=custom_project
        )

        # Verify - should use custom_project not benchmark_dir
        assert resolved == custom_project / "fuzz.c"
        assert resolved.exists()

    def test_resolve_project_variable_missing_file(self, tmp_path):
        """Test error when $PROJECT file doesn't exist."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)

        # Test
        with pytest.raises(FileNotFoundError) as exc_info:
            resolve_harness_path(
                "$PROJECT/missing.c",
                benchmark_dir=benchmark
            )

        # Verify error message
        error_msg = str(exc_info.value)
        assert "Harness file not found" in error_msg
        assert "missing.c" in error_msg
        assert "$PROJECT/missing.c" in error_msg


# ============================================================================
# Test Absolute Path Handling
# ============================================================================

class TestAbsolutePathHandling:
    """Test handling of absolute paths (container paths)."""

    def test_absolute_path_returned_as_is(self, tmp_path):
        """Test that absolute paths are returned without modification."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)

        # Test
        resolved = resolve_harness_path(
            "/src/container/harness.c",
            benchmark_dir=benchmark
        )

        # Verify - absolute paths are not validated, just returned
        assert resolved == Path("/src/container/harness.c")

    def test_absolute_path_no_validation(self, tmp_path):
        """Test that absolute paths are not validated for existence."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)

        # Test - should not raise error even if path doesn't exist
        resolved = resolve_harness_path(
            "/nonexistent/path/to/harness.c",
            benchmark_dir=benchmark
        )

        # Verify
        assert resolved == Path("/nonexistent/path/to/harness.c")


# ============================================================================
# Test Relative Path Handling
# ============================================================================

class TestRelativePathHandling:
    """Test handling of relative paths (./...)."""

    def test_relative_path_basic(self, tmp_path):
        """Test basic relative path resolution."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        harness_file = benchmark / "test" / "harness.c"
        harness_file.parent.mkdir()
        harness_file.write_text("// relative harness")

        # Test
        resolved = resolve_harness_path(
            "./test/harness.c",
            benchmark_dir=benchmark
        )

        # Verify
        assert resolved == harness_file
        assert resolved.exists()
        assert resolved.is_absolute()

    def test_relative_path_missing_file(self, tmp_path):
        """Test error when relative path file doesn't exist."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)

        # Test
        with pytest.raises(FileNotFoundError) as exc_info:
            resolve_harness_path(
                "./test/missing.c",
                benchmark_dir=benchmark
            )

        # Verify error message
        error_msg = str(exc_info.value)
        assert "Harness file not found" in error_msg
        assert "./test/missing.c" in error_msg


# ============================================================================
# Test Harness Source Path Resolution
# ============================================================================

class TestHarnessSourcePathResolution:
    """Test harness source path resolution for CRS arguments."""

    def test_get_harness_source_path_repo_variable(self, tmp_path):
        """Test harness source path resolution for $REPO path."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        repos_dir = tmp_path / "repos"

        harness = HarnessFile(name="ossfuzz", path="$REPO/test/harness.c")

        # Test
        harness_path = get_harness_source_path(
            harness,
            benchmark_dir=benchmark,
            repos_dir=repos_dir
        )

        # Verify
        assert harness_path == repo / "test" / "harness.c"
        assert harness_path.exists()

    def test_get_harness_source_path_project_variable(self, tmp_path):
        """Test harness source path resolution for $PROJECT path."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        harness_file = benchmark / "fuzz.cpp"
        harness_file.write_text("// project harness")

        harness = HarnessFile(name="customfuzz", path="$PROJECT/fuzz.cpp")

        # Test
        harness_path = get_harness_source_path(
            harness,
            benchmark_dir=benchmark
        )

        # Verify
        assert harness_path == harness_file
        assert harness_path.exists()

    def test_get_harness_source_path_failure_returns_none(self, tmp_path):
        """Test that resolution failure returns None instead of raising."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        harness = HarnessFile(name="missing", path="$REPO/missing.c")

        # Test - should return None instead of raising
        harness_path = get_harness_source_path(
            harness,
            benchmark_dir=benchmark,
            repos_dir=tmp_path / "nonexistent"
        )

        # Verify
        assert harness_path is None

    def test_get_harness_source_path_with_different_extensions(self, tmp_path):
        """Test harness source path resolution with various file extensions."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)

        test_cases = [
            "harness.c",
            "harness.cpp",
            "Fuzzer.java",
            "test.go",
        ]

        for filename in test_cases:
            harness_file = benchmark / filename
            harness_file.write_text("// test")

            harness = HarnessFile(name="test", path=f"$PROJECT/{filename}")

            # Test
            harness_path = get_harness_source_path(
                harness,
                benchmark_dir=benchmark
            )

            # Verify
            assert harness_path == harness_file
            assert harness_path.exists()
            assert harness_path.suffix == Path(filename).suffix


# ============================================================================
# Test Error Handling
# ============================================================================

class TestErrorHandling:
    """Test error handling and validation."""

    def test_invalid_path_format_no_prefix(self, tmp_path):
        """Test error on invalid path format (missing prefix)."""
        benchmark = create_test_benchmark(tmp_path)

        with pytest.raises(ValueError) as exc_info:
            resolve_harness_path(
                "test/harness.c",  # Invalid: missing ./ prefix
                benchmark_dir=benchmark
            )

        error_msg = str(exc_info.value)
        assert "Invalid harness path format" in error_msg
        assert "test/harness.c" in error_msg

    def test_invalid_path_format_empty_string(self, tmp_path):
        """Test error on empty path string."""
        benchmark = create_test_benchmark(tmp_path)

        with pytest.raises(ValueError):
            resolve_harness_path(
                "",
                benchmark_dir=benchmark
            )

    def test_repository_error_with_context(self, tmp_path):
        """Test that RepositoryError provides helpful context."""
        benchmark = create_test_benchmark(tmp_path)

        # Create invalid project.yaml (missing main_repo)
        (benchmark / "project.yaml").write_text("language: c\n")

        with pytest.raises(RepositoryError) as exc_info:
            resolve_harness_path(
                "$REPO/test.c",
                benchmark_dir=benchmark,
                repos_dir=tmp_path / "repos"
            )

        error_msg = str(exc_info.value)
        # Should get error related to repository failure
        assert ("Failed to obtain repository" in error_msg or
                "repo_manager returned None" in error_msg)


# ============================================================================
# Test Integration with repo_manager
# ============================================================================

class TestRepoManagerIntegration:
    """Test integration with repo_manager module."""

    @patch('crsbench.evaluation.path_resolver.ensure_project_repository')
    def test_calls_ensure_project_repository(self, mock_ensure_repo, tmp_path):
        """Test that _resolve_repo_path calls ensure_project_repository."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        mock_ensure_repo.return_value = str(repo)

        # Test
        result = _resolve_repo_path(
            benchmark_dir=benchmark,
            repos_dir=tmp_path / "repos",
            project_dir=None
        )

        # Verify
        mock_ensure_repo.assert_called_once()
        assert result == repo

    def test_uses_explicit_project_dir_without_repo_manager(self, tmp_path):
        """Test that explicit project_dir bypasses repo_manager."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        explicit_repo = tmp_path / "explicit" / "repo"
        explicit_repo.mkdir(parents=True)

        # Test
        with patch('crsbench.evaluation.path_resolver.ensure_project_repository') as mock:
            result = _resolve_repo_path(
                benchmark_dir=benchmark,
                repos_dir=None,
                project_dir=explicit_repo
            )

            # Verify repo_manager was NOT called
            mock.assert_not_called()
            assert result == explicit_repo

    def test_explicit_project_dir_not_exists_error(self, tmp_path):
        """Test error when explicit project_dir doesn't exist."""
        benchmark = create_test_benchmark(tmp_path)
        nonexistent = tmp_path / "nonexistent"

        with pytest.raises(FileNotFoundError) as exc_info:
            _resolve_repo_path(
                benchmark_dir=benchmark,
                repos_dir=None,
                project_dir=nonexistent
            )

        assert "Explicit project directory not found" in str(exc_info.value)


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests with realistic scenarios."""

    def test_end_to_end_repo_resolution(self, tmp_path):
        """Test complete end-to-end workflow for $REPO resolution."""
        # Setup complete benchmark structure
        benchmark = create_test_benchmark(tmp_path, "libxml2")
        repo = create_test_repository(tmp_path, "libxml2")
        repos_dir = tmp_path / "repos"

        # Create harness object
        harness = HarnessFile(
            name="xml_parser",
            path="$REPO/test/harness.c"
        )

        # Test resolution
        harness_path = get_harness_source_path(
            harness,
            benchmark_dir=benchmark,
            repos_dir=repos_dir
        )

        # Verify
        assert harness_path is not None
        assert harness_path.exists()
        assert harness_path == repo / "test" / "harness.c"

        # Verify can be used in CRS command
        cmd_arg = ["--harness-source", str(harness_path)]
        assert cmd_arg[0] == "--harness-source"
        assert str(harness_path) in cmd_arg[1]

    def test_multiple_harnesses_same_benchmark(self, tmp_path):
        """Test resolving multiple harnesses from same benchmark."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        repos_dir = tmp_path / "repos"

        # Create additional harness files
        (benchmark / "fuzz1.c").write_text("// fuzz1")
        (benchmark / "fuzz2.cpp").write_text("// fuzz2")

        harnesses = [
            HarnessFile(name="repo_harness", path="$REPO/test/harness.c"),
            HarnessFile(name="project_harness1", path="$PROJECT/fuzz1.c"),
            HarnessFile(name="project_harness2", path="$PROJECT/fuzz2.cpp"),
        ]

        # Test - resolve all harnesses
        resolved_paths = []
        for harness in harnesses:
            harness_path = get_harness_source_path(
                harness,
                benchmark_dir=benchmark,
                repos_dir=repos_dir
            )
            resolved_paths.append(harness_path)

        # Verify all resolved successfully
        assert len(resolved_paths) == 3
        assert all(path is not None for path in resolved_paths)
        assert all(path.exists() for path in resolved_paths)

    def test_harness_path_with_deeply_nested_structure(self, tmp_path):
        """Test resolution with deeply nested directory structure."""
        # Setup
        benchmark = create_test_benchmark(tmp_path)
        repo = create_test_repository(tmp_path)
        repos_dir = tmp_path / "repos"

        # Create deeply nested harness
        nested_path = repo / "src" / "test" / "fuzzing" / "harnesses"
        nested_path.mkdir(parents=True)
        (nested_path / "deep_harness.c").write_text("// deep")

        # Test
        resolved = resolve_harness_path(
            "$REPO/src/test/fuzzing/harnesses/deep_harness.c",
            benchmark_dir=benchmark,
            repos_dir=repos_dir
        )

        # Verify
        assert resolved.exists()
        assert resolved == nested_path / "deep_harness.c"
