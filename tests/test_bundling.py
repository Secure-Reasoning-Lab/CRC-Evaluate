"""Tests for crsbench.benchmark.packaging module."""

from pathlib import Path
from unittest.mock import patch

from crsbench.benchmark.packaging.bundle import bundle_benchmark
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

    # Standard Dockerfile with COPY pkgs/ pattern for valid benchmarks
    VALID_DOCKERFILE = (
        "FROM base\n"
        "COPY pkgs/test.tar.gz $SRC/test.tar.gz\n"
        "RUN tar -xzf $SRC/test.tar.gz && rm $SRC/test.tar.gz\n"
        "WORKDIR $SRC/test\n"
    )

    def _add_pkgs(self, path: Path) -> None:
        """Add pkgs/ directory with test.tar.gz and pkg_refs.txt."""
        pkgs = path / "pkgs"
        pkgs.mkdir(exist_ok=True)
        (pkgs / "test.tar.gz").write_text("dummy")
        (pkgs / "pkg_refs.txt").write_text("https://github.com/test/repo@abc123\n")

    def test_valid_benchmark(self, tmp_path: Path) -> None:
        """Test validation of valid benchmark."""
        # Create minimal valid benchmark (ref_commit is required for all)
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        self._add_pkgs(tmp_path)

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
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("language: c\n")  # No main_repo
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid  # Still valid, but warning
        assert any("main_repo" in w for w in result.warnings)

    def test_warns_missing_ref_commit_flat_format(self, tmp_path: Path) -> None:
        """Test validation warns when ref_commit is missing in flat format (full-only mode)."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        # Flat format without ref_commit is valid but treated as full-only
        assert result.valid
        assert any("ref_commit" in w for w in result.warnings)

    def test_validates_pkgs_structure(self, tmp_path: Path) -> None:
        """Test validation of pkgs/ directory structure with wrong tarball name."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        (tmp_path / "pkgs").mkdir()
        # Wrong tarball name (should be test.tar.gz to match WORKDIR)
        (tmp_path / "pkgs" / "wrong.tar.gz").write_text("dummy")

        result = validate_benchmark(tmp_path)
        # Missing tarball is now an error (VAL-02)
        assert not result.valid
        assert any(
            "expected" in e.lower() and "test.tar.gz" in e.lower()
            for e in result.errors
        )

    def test_warns_missing_pkg_refs(self, tmp_path: Path) -> None:
        """Test validation warns about missing pkg_refs.txt."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
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
    """Tests for get_benchmark_info function.

    Note: These tests use MetaYamlAdapter which requires:
    - Valid commit hash format (7-40 hex characters)
    - harness_files field (at least one entry)
    """

    # Minimal harness_files for valid meta.yaml
    HARNESS_FILES = (
        "harness_files:\n  - name: test_harness\n    path: /src/test/harness\n"
    )

    def test_flat_format_not_supported(self, tmp_path: Path) -> None:
        """Test that flat format (legacy) is no longer supported.

        All benchmarks now use nested format (delta_mode/full_mode sections).
        Flat format with just base_commit/ref_commit at top level is rejected.
        """
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        # Flat format - no delta_mode or full_mode section
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            f"base_commit: abc1234\nref_commit: def5678\n{self.HARNESS_FILES}"
        )

        info = get_benchmark_info(tmp_path)

        # Flat format is rejected by Pydantic validation
        assert info is None

    def test_nested_delta_mode(self, tmp_path: Path) -> None:
        """Test extraction from nested delta_mode format."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            f"delta_mode:\n  base_commit: abc1234\n  ref_commit: def5678\n{self.HARNESS_FILES}"
        )

        info = get_benchmark_info(tmp_path)

        assert info is not None
        assert info["base_commit"] == "abc1234"
        assert info["ref_commit"] == "def5678"
        assert info["has_delta_mode"] is True

    def test_nested_full_mode(self, tmp_path: Path) -> None:
        """Test extraction from nested full_mode format (no ref_commit needed)."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            f"full_mode:\n  base_commit: abc1234def5678\n{self.HARNESS_FILES}"
        )

        info = get_benchmark_info(tmp_path)

        assert info is not None
        assert info["base_commit"] == "abc1234def5678"
        # Full-only mode doesn't have ref_commit (base IS the vulnerable state)
        assert "ref_commit" not in info
        assert info["has_delta_mode"] is False

    def test_both_modes_prefers_delta_for_bundling(self, tmp_path: Path) -> None:
        """Test that delta_mode is preferred for bundling when both modes exist."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: aaa1111\n"
            "  ref_commit: bbb2222\n"
            "full_mode:\n"
            "  base_commit: ccc3333\n"
            f"{self.HARNESS_FILES}"
        )

        info = get_benchmark_info(tmp_path)

        # Delta mode is preferred for bundling because it allows ref.diff generation
        assert info is not None
        assert info["has_delta_mode"] is True
        assert info["base_commit"] == "aaa1111"
        assert info["ref_commit"] == "bbb2222"

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

    VALID_DOCKERFILE = (
        "FROM base\nCOPY pkgs/test.tar.gz $SRC/test.tar.gz\nWORKDIR $SRC/test\n"
    )

    def _add_pkgs(self, path: Path) -> None:
        pkgs = path / "pkgs"
        pkgs.mkdir(exist_ok=True)
        (pkgs / "test.tar.gz").write_text("dummy")
        (pkgs / "pkg_refs.txt").write_text("ref\n")

    def test_valid_delta_mode(self, tmp_path: Path) -> None:
        """Test validation of valid delta_mode section."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert len(result.errors) == 0

    def test_valid_full_mode(self, tmp_path: Path) -> None:
        """Test validation of valid full_mode section (no ref_commit needed)."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "full_mode:\n  base_commit: abc123\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid

    def test_delta_mode_missing_base_commit(self, tmp_path: Path) -> None:
        """Test validation catches missing base_commit in delta_mode."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  ref_commit: def456\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert any("delta_mode missing base_commit" in e for e in result.errors)

    def test_delta_mode_missing_ref_commit(self, tmp_path: Path) -> None:
        """Test validation catches missing ref_commit in delta_mode."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert not result.valid
        assert any("delta_mode missing ref_commit" in e for e in result.errors)

    def test_full_mode_valid_without_ref_commit(self, tmp_path: Path) -> None:
        """Test validation accepts full_mode without ref_commit (base IS the vulnerable state)."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "full_mode:\n  base_commit: abc123\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        # Full mode doesn't need ref_commit - base_commit IS the vulnerable state
        assert result.valid
        assert len(result.errors) == 0

    def test_both_modes_valid(self, tmp_path: Path) -> None:
        """Test validation passes with both modes defined."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: delta_base\n"
            "  ref_commit: delta_ref\n"
            "full_mode:\n"
            "  base_commit: full_base\n"
        )
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid


class TestValidateRefDiff:
    """Tests for ref.diff validation in delta mode benchmarks."""

    VALID_DOCKERFILE = (
        "FROM base\nCOPY pkgs/test.tar.gz $SRC/test.tar.gz\nWORKDIR $SRC/test\n"
    )

    def _add_pkgs(self, path: Path) -> None:
        pkgs = path / "pkgs"
        pkgs.mkdir(exist_ok=True)
        (pkgs / "test.tar.gz").write_text("dummy")
        (pkgs / "pkg_refs.txt").write_text("ref\n")

    def test_delta_mode_with_ref_diff_no_warning(self, tmp_path: Path) -> None:
        """Test delta mode benchmark with ref.diff has no warning."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )
        (tmp_path / ".aixcc" / "ref.diff").write_text("--- a/file\n+++ b/file\n")
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert not any("ref.diff" in w for w in result.warnings)

    def test_delta_mode_without_ref_diff_warns(self, tmp_path: Path) -> None:
        """Test delta mode benchmark without ref.diff emits warning."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )
        # No ref.diff file
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid  # Missing ref.diff is warning, not error
        assert any("ref.diff" in w for w in result.warnings)
        assert any("prepare-delta" in w for w in result.warnings)

    def test_full_mode_no_ref_diff_check(self, tmp_path: Path) -> None:
        """Test full mode benchmark doesn't require ref.diff."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "full_mode:\n  base_commit: abc123\n"
        )
        # No ref.diff file - should be fine for full mode
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid
        # Should NOT warn about missing ref.diff for full mode
        assert not any("ref.diff" in w for w in result.warnings)

    def test_flat_format_with_ref_commit_warns_missing_ref_diff(
        self, tmp_path: Path
    ) -> None:
        """Test flat format with ref_commit (legacy delta) warns about missing ref.diff."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        # Flat format with ref_commit indicates delta mode
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "base_commit: abc123\nref_commit: def456\n"
        )
        # No ref.diff file
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid
        assert any("ref.diff" in w for w in result.warnings)

    def test_flat_format_without_ref_commit_no_ref_diff_warning(
        self, tmp_path: Path
    ) -> None:
        """Test flat format without ref_commit (full-only) doesn't warn about ref.diff."""
        (tmp_path / "Dockerfile").write_text(self.VALID_DOCKERFILE)
        (tmp_path / "project.yaml").write_text("main_repo: test\n")
        (tmp_path / ".aixcc").mkdir()
        # Flat format without ref_commit is full-only mode
        (tmp_path / ".aixcc" / "meta.yaml").write_text("base_commit: abc123\n")
        # No ref.diff file - should be fine for full-only
        self._add_pkgs(tmp_path)

        result = validate_benchmark(tmp_path)
        assert result.valid
        # Should warn about missing ref_commit (full-only), but NOT about ref.diff
        assert any("ref_commit" in w for w in result.warnings)
        assert not any("ref.diff" in w for w in result.warnings)


class TestBundleBenchmark:
    """Tests for bundle_benchmark function."""

    # Minimal harness_files for valid meta.yaml
    HARNESS_FILES = (
        "harness_files:\n  - name: test_harness\n    path: /src/test/harness\n"
    )

    def _create_valid_benchmark(self, tmp_path: Path) -> Path:
        """Create a valid benchmark structure for testing."""
        (tmp_path / "Dockerfile").write_text(
            "FROM base\nCOPY pkgs/test.tar.gz $SRC/test.tar.gz\nWORKDIR $SRC/test\n"
        )
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: abc1234\n"
            "  ref_commit: def5678\n"
            f"{self.HARNESS_FILES}"
        )
        return tmp_path

    def test_skips_when_tarball_exists_without_force(self, tmp_path: Path) -> None:
        """Test bundle skips when source tarball and pkg_refs match."""
        benchmark = self._create_valid_benchmark(tmp_path)

        # Pre-create pkgs/ with source tarball and matching pkg_refs.txt
        pkgs_dir = benchmark / "pkgs"
        pkgs_dir.mkdir()
        source_tarball = pkgs_dir / "test.tar.gz"
        source_tarball.write_text("existing tarball")
        (pkgs_dir / "pkg_refs.txt").write_text("https://github.com/test/repo@def5678\n")

        # Mock create_source_tarball to ensure it's not called
        with patch(
            "crsbench.benchmark.packaging.bundle.create_source_tarball"
        ) as mock_create:
            result = bundle_benchmark(benchmark, force=False)

            # Should return pkgs_dir without calling create_source_tarball
            assert result == pkgs_dir
            mock_create.assert_not_called()

        # Original tarball should be unchanged
        assert source_tarball.read_text() == "existing tarball"

    def test_overwrites_when_tarball_exists_with_force(self, tmp_path: Path) -> None:
        """Test bundle overwrites source tarball when --force is used."""
        benchmark = self._create_valid_benchmark(tmp_path)

        # Pre-create pkgs/ with source tarball
        pkgs_dir = benchmark / "pkgs"
        pkgs_dir.mkdir()
        source_tarball = pkgs_dir / "test.tar.gz"
        source_tarball.write_text("old tarball")

        # Also create ref.diff to test it gets replaced
        aixcc_dir = benchmark / ".aixcc"
        old_ref_diff = aixcc_dir / "ref.diff"
        old_ref_diff.write_text("old ref.diff")

        # Mock create_source_tarball to return new paths
        new_tarball = pkgs_dir / "test.tar.gz"
        new_ref_diff = pkgs_dir / "ref.diff"

        def mock_create_tarball(
            repo_url,  # noqa: ARG001
            base_commit,  # noqa: ARG001
            source_name,  # noqa: ARG001
            output_dir,  # noqa: ARG001
            *,
            ref_commit=None,  # noqa: ARG001
            log_prefix=None,  # noqa: ARG001
        ):
            # Simulate creating new tarball (uses closure vars, not params)
            new_tarball.write_text("new tarball content")
            new_ref_diff.write_text("new ref.diff content")
            return new_tarball, new_ref_diff

        with patch(
            "crsbench.benchmark.packaging.bundle.create_source_tarball",
            side_effect=mock_create_tarball,
        ) as mock_create:
            result = bundle_benchmark(benchmark, force=True)

            # Should have called create_source_tarball
            assert result == pkgs_dir
            mock_create.assert_called_once()

        # Source tarball should be updated
        assert source_tarball.read_text() == "new tarball content"

        # ref.diff should be moved to .aixcc/
        assert (aixcc_dir / "ref.diff").read_text() == "new ref.diff content"

    def test_creates_normally_when_no_existing_tarball(self, tmp_path: Path) -> None:
        """Test bundle creates tarball normally when none exists."""
        benchmark = self._create_valid_benchmark(tmp_path)
        pkgs_dir = benchmark / "pkgs"

        # Mock create_source_tarball
        def mock_create_tarball(
            repo_url,  # noqa: ARG001
            base_commit,  # noqa: ARG001
            source_name,
            output_dir,
            *,
            ref_commit=None,  # noqa: ARG001
            log_prefix=None,  # noqa: ARG001
        ):
            tarball = output_dir / f"{source_name}.tar.gz"
            tarball.write_text("new tarball")
            ref_diff = output_dir / "ref.diff"
            ref_diff.write_text("new diff")
            return tarball, ref_diff

        with patch(
            "crsbench.benchmark.packaging.bundle.create_source_tarball",
            side_effect=mock_create_tarball,
        ) as mock_create:
            result = bundle_benchmark(benchmark, force=False)

            assert result == pkgs_dir
            mock_create.assert_called_once()

        # Verify tarball and pkg_refs.txt created
        assert (pkgs_dir / "test.tar.gz").exists()
        assert (pkgs_dir / "pkg_refs.txt").exists()
        assert (benchmark / ".aixcc" / "ref.diff").exists()

    def test_preserves_dependency_tarballs_with_force(self, tmp_path: Path) -> None:
        """Test bundle preserves other tarballs (dependencies) when using --force."""
        benchmark = self._create_valid_benchmark(tmp_path)

        # Pre-create pkgs/ with source tarball AND dependency tarball
        pkgs_dir = benchmark / "pkgs"
        pkgs_dir.mkdir()
        source_tarball = pkgs_dir / "test.tar.gz"
        source_tarball.write_text("old source")
        dep_tarball = pkgs_dir / "zlib.tar.gz"
        dep_tarball.write_text("zlib dependency")

        def mock_create_tarball(
            repo_url,  # noqa: ARG001
            base_commit,  # noqa: ARG001
            source_name,
            output_dir,
            *,
            ref_commit=None,  # noqa: ARG001
            log_prefix=None,  # noqa: ARG001
        ):
            tarball = output_dir / f"{source_name}.tar.gz"
            tarball.write_text("new source")
            return tarball, None

        with patch(
            "crsbench.benchmark.packaging.bundle.create_source_tarball",
            side_effect=mock_create_tarball,
        ):
            bundle_benchmark(benchmark, force=True)

        # Dependency tarball should be preserved
        assert dep_tarball.exists()
        assert dep_tarball.read_text() == "zlib dependency"

        # Source tarball should be updated
        assert source_tarball.read_text() == "new source"

    def test_full_mode_no_ref_diff_generated(self, tmp_path: Path) -> None:
        """Test bundle for full-mode benchmark (no ref.diff)."""
        (tmp_path / "Dockerfile").write_text(
            "FROM base\nCOPY pkgs/test.tar.gz $SRC/test.tar.gz\nWORKDIR $SRC/test\n"
        )
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/test/repo\n"
        )
        (tmp_path / ".aixcc").mkdir()
        # Full mode only - no ref_commit
        (tmp_path / ".aixcc" / "meta.yaml").write_text(
            f"full_mode:\n  base_commit: abc1234\n{self.HARNESS_FILES}"
        )
        pkgs_dir = tmp_path / "pkgs"

        def mock_create_tarball(
            repo_url,  # noqa: ARG001
            base_commit,  # noqa: ARG001
            source_name,
            output_dir,
            *,
            ref_commit=None,  # noqa: ARG001
            log_prefix=None,  # noqa: ARG001
        ):
            tarball = output_dir / f"{source_name}.tar.gz"
            tarball.write_text("tarball content")
            # Full mode: no ref_commit, so no ref.diff
            return tarball, None

        with patch(
            "crsbench.benchmark.packaging.bundle.create_source_tarball",
            side_effect=mock_create_tarball,
        ) as mock_create:
            result = bundle_benchmark(tmp_path, force=False)

            # ref_commit should be None for full mode
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args
            assert call_kwargs[1].get("ref_commit") is None

        # No ref.diff should exist
        assert not (tmp_path / ".aixcc" / "ref.diff").exists()
