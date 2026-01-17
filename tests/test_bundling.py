"""Tests for crsbench.benchmark.packaging module."""

from pathlib import Path

from crsbench.benchmark.packaging.validate import (
    ValidationResult,
    get_benchmark_info,
    validate_benchmark,
)
from crsbench.benchmark.packaging.workdir_parser import (
    _normalize_workdir,
    get_expected_source_dir,
)


class TestWorkdirParser:
    """Tests for WORKDIR parsing from Dockerfile."""

    def test_absolute_src_path(self, tmp_path: Path) -> None:
        """Test parsing WORKDIR $SRC/curl."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR $SRC/curl\n")

        result = get_expected_source_dir(dockerfile)
        assert result == "curl"

    def test_absolute_src_path_with_braces(self, tmp_path: Path) -> None:
        """Test parsing WORKDIR ${SRC}/curl."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR ${SRC}/curl\n")

        result = get_expected_source_dir(dockerfile)
        assert result == "curl"

    def test_absolute_slash_src_path(self, tmp_path: Path) -> None:
        """Test parsing WORKDIR /src/curl."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR /src/curl\n")

        result = get_expected_source_dir(dockerfile)
        assert result == "curl"

    def test_relative_workdir(self, tmp_path: Path) -> None:
        """Test parsing relative WORKDIR libtiff."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR /src\nWORKDIR libtiff\n")

        result = get_expected_source_dir(dockerfile)
        assert result == "libtiff"

    def test_last_workdir_wins(self, tmp_path: Path) -> None:
        """Test that last WORKDIR is used."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(
            "FROM base\n"
            "WORKDIR /src\n"
            "WORKDIR $SRC/first\n"
            "RUN echo 'building'\n"
            "WORKDIR $SRC/second\n"
        )

        result = get_expected_source_dir(dockerfile)
        assert result == "second"

    def test_no_workdir(self, tmp_path: Path) -> None:
        """Test Dockerfile without WORKDIR."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nRUN echo hello\n")

        result = get_expected_source_dir(dockerfile)
        assert result is None

    def test_nonexistent_dockerfile(self, tmp_path: Path) -> None:
        """Test handling of nonexistent Dockerfile."""
        dockerfile = tmp_path / "Dockerfile"

        result = get_expected_source_dir(dockerfile)
        assert result is None

    def test_comments_ignored(self, tmp_path: Path) -> None:
        """Test that commented WORKDIR is ignored."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(
            "FROM base\n# WORKDIR $SRC/commented\nWORKDIR $SRC/actual\n"
        )

        result = get_expected_source_dir(dockerfile)
        assert result == "actual"

    def test_case_insensitive(self, tmp_path: Path) -> None:
        """Test case-insensitive WORKDIR matching."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nworkdir $SRC/lower\n")

        result = get_expected_source_dir(dockerfile)
        assert result == "lower"


class TestNormalizeWorkdir:
    """Tests for WORKDIR normalization."""

    def test_src_variable(self) -> None:
        assert _normalize_workdir("$SRC/curl") == "curl"

    def test_src_with_braces(self) -> None:
        assert _normalize_workdir("${SRC}/curl") == "curl"

    def test_absolute_src(self) -> None:
        assert _normalize_workdir("/src/curl") == "curl"

    def test_relative_name(self) -> None:
        assert _normalize_workdir("libtiff") == "libtiff"

    def test_nested_path(self) -> None:
        assert _normalize_workdir("$SRC/some/nested/path") == "path"


class TestValidateBenchmark:
    """Tests for benchmark validation."""

    def test_valid_benchmark(self, tmp_path: Path) -> None:
        """Test validation of valid benchmark."""
        # Create minimal valid benchmark
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert len(result.errors) == 0

    def test_missing_dockerfile(self, tmp_path: Path) -> None:
        """Test validation catches missing Dockerfile."""
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert "Missing Dockerfile" in result.errors

    def test_missing_project_yaml(self, tmp_path: Path) -> None:
        """Test validation catches missing project.yaml."""
        (tmp_path / "Dockerfile").write_text("FROM base\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert "Missing project.yaml" in result.errors

    def test_missing_meta_yaml(self, tmp_path: Path) -> None:
        """Test validation catches missing meta.yaml."""
        (tmp_path / "Dockerfile").write_text("FROM base\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert "Missing .aixcc/meta.yaml" in result.errors

    def test_warns_missing_main_repo(self, tmp_path: Path) -> None:
        """Test validation warns about missing main_repo."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("language: c\n")  # No main_repo
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        result = validate_benchmark(tmp_path)
        assert result.valid  # Still valid, but warning
        assert any("main_repo" in w for w in result.warnings)

    def test_warns_missing_ref_commit(self, tmp_path: Path) -> None:
        """Test validation warns about missing ref_commit."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert any("ref_commit" in w for w in result.warnings)

    def test_validates_pkgs_structure(self, tmp_path: Path) -> None:
        """Test validation of pkgs/ directory structure."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        (tmp_path / "pkgs").mkdir()
        # Wrong tarball name (should be test.tar.gz to match WORKDIR)
        (tmp_path / "pkgs" / "wrong.tar.gz").write_text("dummy")

        result = validate_benchmark(tmp_path)
        assert result.valid  # pkgs issues are warnings, not errors
        assert any(
            "expected tarball" in w.lower() or "test.tar.gz" in w
            for w in result.warnings
        )

    def test_warns_missing_pkg_refs(self, tmp_path: Path) -> None:
        """Test validation warns about missing pkg_refs.txt."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        (tmp_path / "pkgs").mkdir()
        (tmp_path / "pkgs" / "test.tar.gz").write_text("dummy")
        # No pkg_refs.txt

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert any("pkg_refs.txt" in w for w in result.warnings)

    def test_not_a_directory(self, tmp_path: Path) -> None:
        """Test validation of non-directory path."""
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("test")

        result = validate_benchmark(file_path)
        assert not result.valid
        assert any("Not a directory" in e for e in result.errors)


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_str_valid(self) -> None:
        result = ValidationResult(valid=True)
        assert str(result) == "Valid"

    def test_str_with_errors(self) -> None:
        result = ValidationResult(valid=False, errors=["error1", "error2"])
        assert "error1" in str(result)
        assert "error2" in str(result)

    def test_str_with_warnings(self) -> None:
        result = ValidationResult(valid=True, warnings=["warning1"])
        assert "warning1" in str(result)


class TestGetBenchmarkInfo:
    """Tests for get_benchmark_info function."""

    def test_flat_format(self, tmp_path: Path) -> None:
        """Test extraction from flat meta.yaml format."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )

        info = get_benchmark_info(tmp_path)

        assert info is not None
        assert info["main_repo"] == "https://github.com/test/repo"
        assert info["base_commit"] == "abc123"
        assert info["ref_commit"] == "def456"

    def test_nested_delta_mode(self, tmp_path: Path) -> None:
        """Test extraction from nested delta_mode format."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )

        info = get_benchmark_info(tmp_path, mode="delta")

        assert info is not None
        assert info["base_commit"] == "abc123"
        assert info["ref_commit"] == "def456"

    def test_nested_full_mode(self, tmp_path: Path) -> None:
        """Test extraction from nested full_mode format."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "full_mode:\n  base_commit: fullbase123\n"
        )

        info = get_benchmark_info(tmp_path, mode="full")

        assert info is not None
        assert info["base_commit"] == "fullbase123"
        assert "ref_commit" not in info

    def test_both_modes_prefers_requested(self, tmp_path: Path) -> None:
        """Test that requested mode is used when both exist."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: delta_base\n"
            "  ref_commit: delta_ref\n"
            "full_mode:\n"
            "  base_commit: full_base\n"
        )

        delta_info = get_benchmark_info(tmp_path, mode="delta")
        full_info = get_benchmark_info(tmp_path, mode="full")

        assert delta_info["base_commit"] == "delta_base"
        assert delta_info["ref_commit"] == "delta_ref"
        assert full_info["base_commit"] == "full_base"
        assert "ref_commit" not in full_info

    def test_missing_main_repo_returns_none(self, tmp_path: Path) -> None:
        """Test returns None when main_repo missing."""
        (tmp_path / "project.yaml").write_text("language: c\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")

        info = get_benchmark_info(tmp_path)

        assert info is None

    def test_missing_base_commit_returns_none(self, tmp_path: Path) -> None:
        """Test returns None when base_commit missing."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("ref_commit: def456\n")

        info = get_benchmark_info(tmp_path)

        assert info is None

    def test_missing_files_returns_none(self, tmp_path: Path) -> None:
        """Test returns None when required files missing."""
        info = get_benchmark_info(tmp_path)
        assert info is None


class TestValidateNestedMetaYaml:
    """Tests for nested meta.yaml format validation."""

    def test_valid_delta_mode(self, tmp_path: Path) -> None:
        """Test validation of valid delta_mode section."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert len(result.errors) == 0

    def test_valid_full_mode(self, tmp_path: Path) -> None:
        """Test validation of valid full_mode section."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "full_mode:\n  base_commit: abc123\n"
        )

        result = validate_benchmark(tmp_path)
        assert result.valid

    def test_delta_mode_missing_base_commit(self, tmp_path: Path) -> None:
        """Test validation catches missing base_commit in delta_mode."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  ref_commit: def456\n"
        )

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert any("delta_mode missing base_commit" in e for e in result.errors)

    def test_delta_mode_missing_ref_commit(self, tmp_path: Path) -> None:
        """Test validation catches missing ref_commit in delta_mode."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n"
        )

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert any("delta_mode missing ref_commit" in e for e in result.errors)

    def test_both_modes_valid(self, tmp_path: Path) -> None:
        """Test validation passes with both modes defined."""
        (tmp_path / "Dockerfile").write_text("FROM base\nWORKDIR $SRC/test\n")
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: delta_base\n"
            "  ref_commit: delta_ref\n"
            "full_mode:\n"
            "  base_commit: full_base\n"
        )

        result = validate_benchmark(tmp_path)
        assert result.valid
