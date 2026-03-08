"""Tests for packaging validation rules (VAL-01, VAL-02).

Tests the strict validation rules that ensure all benchmarks:
- Have COPY/ADD pkgs/ pattern in their Dockerfile (VAL-01)
- Have pkgs/ directory with matching source tarballs (VAL-02)
"""

from pathlib import Path

import pytest
from crsbench.benchmark.packaging.validate import (
    validate_benchmark,
    validate_tarball_naming,
)

# Standard valid Dockerfile template
VALID_DOCKERFILE = """\
FROM ghcr.io/aixcc-finals/base-builder:v1.3.0
COPY pkgs/foo.tar.gz $SRC/foo.tar.gz
RUN tar -xzf $SRC/foo.tar.gz && rm $SRC/foo.tar.gz
WORKDIR $SRC/foo
"""

LEGACY_PATH_TEST_SH = """\
#!/bin/bash
cd /built-src/foo
"""

LEGACY_PATH_COMMENT_ONLY_TEST_SH = """\
#!/bin/bash
# historical note: migrated from /built-src/foo
echo ok
"""

LEGACY_PATH_INLINE_COMMENT_TEST_SH = """\
#!/bin/bash
echo ok # historical note: migrated from /built-src/foo
"""

LEGACY_PATH_PARAMETER_EXPANSION_TEST_SH = """\
#!/bin/bash
x=${var#/built-src/foo}
echo "$x"
"""

LEGACY_PATH_EMBEDDED_WORD_TEST_SH = """\
#!/bin/bash
echo prefix#/built-src/foo
"""

# Dockerfile with ADD instead of COPY
ADD_DOCKERFILE = """\
FROM ghcr.io/aixcc-finals/base-builder:v1.3.0
ADD pkgs/foo.tar.gz $SRC/
WORKDIR $SRC/foo
"""

# Dockerfile missing COPY/ADD pkgs/ pattern
NO_PKGS_DOCKERFILE = """\
FROM ghcr.io/aixcc-finals/base-builder:v1.3.0
RUN echo building
WORKDIR $SRC/foo
"""

# Dockerfile with commented-out COPY pkgs/
COMMENTED_DOCKERFILE = """\
FROM ghcr.io/aixcc-finals/base-builder:v1.3.0
# COPY pkgs/foo.tar.gz $SRC/foo.tar.gz
WORKDIR $SRC/foo
"""


def _create_benchmark(
    tmp_path: Path,
    *,
    dockerfile_content: str,
    has_pkgs: bool = True,
    tarball_name: str | None = None,
    has_pkg_refs: bool = True,
) -> Path:
    """Create minimal benchmark directory for testing."""
    bm = tmp_path / "test-benchmark"
    bm.mkdir()
    (bm / "Dockerfile").write_text(dockerfile_content)
    (bm / "project.yaml").write_text("main_repo: https://github.com/test/repo\n")
    aixcc = bm / ".aixcc"
    aixcc.mkdir()
    (aixcc / "meta.yaml").write_text("full_mode:\n  base_commit: abc123\n")
    if has_pkgs:
        pkgs = bm / "pkgs"
        pkgs.mkdir()
        if tarball_name:
            (pkgs / tarball_name).write_text("fake tarball")
        if has_pkg_refs:
            (pkgs / "pkg_refs.txt").write_text("https://github.com/test/repo@abc123\n")
    return bm


# ============================================================================
# VAL-01: Dockerfile must contain COPY/ADD pkgs/ pattern
# ============================================================================


class TestDockerfilePkgsPattern:
    """Tests for VAL-01: Dockerfile COPY/ADD pkgs/ pattern validation."""

    def test_validate_dockerfile_has_copy_pkgs_passes(self, tmp_path: Path) -> None:
        """Benchmark with COPY pkgs/ in Dockerfile passes validation."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert result.valid
        assert not any("COPY/ADD pkgs/" in e for e in result.errors)

    def test_validate_dockerfile_has_add_pkgs_passes(self, tmp_path: Path) -> None:
        """Benchmark with ADD pkgs/ in Dockerfile passes validation."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=ADD_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert result.valid
        assert not any("COPY/ADD pkgs/" in e for e in result.errors)

    def test_validate_dockerfile_missing_pkgs_pattern_fails(
        self, tmp_path: Path
    ) -> None:
        """Benchmark without COPY/ADD pkgs/ in Dockerfile returns error."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=NO_PKGS_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("COPY/ADD pkgs/" in e for e in result.errors)

    def test_validate_dockerfile_commented_copy_pkgs_fails(
        self, tmp_path: Path
    ) -> None:
        """Benchmark with commented-out COPY pkgs/ returns error."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=COMMENTED_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("COPY/ADD pkgs/" in e for e in result.errors)


# ============================================================================
# VAL-02: pkgs/ directory and tarball validation
# ============================================================================


class TestPkgsDirValidation:
    """Tests for VAL-02: pkgs/ directory and tarball existence."""

    def test_validate_missing_pkgs_dir_fails(self, tmp_path: Path) -> None:
        """Benchmark without pkgs/ directory returns error."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            has_pkgs=False,
        )

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("Missing pkgs/" in e for e in result.errors)

    def test_validate_pkgs_dir_missing_tarball_fails(self, tmp_path: Path) -> None:
        """Benchmark with pkgs/ but no matching tarball returns error."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name=None,  # No tarball at all
        )

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("tarball" in e.lower() for e in result.errors)

    def test_validate_pkgs_dir_with_correct_tarball_passes(
        self, tmp_path: Path
    ) -> None:
        """Benchmark with pkgs/{workdir_name}.tar.gz passes."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert result.valid
        assert len(result.errors) == 0


# ============================================================================
# Integration Tests
# ============================================================================


class TestPackagingValidationIntegration:
    """Integration tests combining VAL-01 and VAL-02."""

    def test_validate_complete_benchmark_passes(self, tmp_path: Path) -> None:
        """Full valid benchmark passes all validation checks."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )

        result = validate_benchmark(bm)
        assert result.valid
        assert len(result.errors) == 0

    def test_validate_tarball_naming_wired_in(self, tmp_path: Path) -> None:
        """Benchmark with misnamed tarball returns error from validate_tarball_naming."""
        # Create Dockerfile with WORKDIR $SRC/foo but put wrong tarball in pkgs/
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="bar.tar.gz",  # Wrong name (should be foo.tar.gz)
        )

        result = validate_benchmark(bm)
        assert not result.valid
        # Should have errors about the tarball mismatch
        assert any("foo.tar.gz" in e for e in result.errors)

    def test_validate_legacy_source_path_fails(self, tmp_path: Path) -> None:
        """Legacy /built-src or /test-src usage in scripts should fail validation."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )
        (bm / "test.sh").write_text(LEGACY_PATH_TEST_SH)

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("Forbidden legacy source paths" in e for e in result.errors)
        assert any("/built-src" in e for e in result.errors)

    def test_validate_legacy_source_path_in_comment_only_passes(
        self, tmp_path: Path
    ) -> None:
        """Comment-only legacy path mentions should not fail validation."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )
        (bm / "test.sh").write_text(LEGACY_PATH_COMMENT_ONLY_TEST_SH)

        result = validate_benchmark(bm)
        assert result.valid

    def test_validate_legacy_source_path_in_inline_comment_passes(
        self, tmp_path: Path
    ) -> None:
        """Inline trailing comments with legacy paths should not fail validation."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )
        (bm / "test.sh").write_text(LEGACY_PATH_INLINE_COMMENT_TEST_SH)

        result = validate_benchmark(bm)
        assert result.valid

    def test_validate_legacy_source_path_in_parameter_expansion_fails(
        self, tmp_path: Path
    ) -> None:
        """Shell parameter expansion with '/built-src' must still be flagged."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )
        (bm / "test.sh").write_text(LEGACY_PATH_PARAMETER_EXPANSION_TEST_SH)

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("Forbidden legacy source paths" in e for e in result.errors)

    def test_validate_legacy_source_path_embedded_word_fails(
        self, tmp_path: Path
    ) -> None:
        """Embedded '#/built-src' token in a word should still be flagged."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
        )
        (bm / "test.sh").write_text(LEGACY_PATH_EMBEDDED_WORD_TEST_SH)

        result = validate_benchmark(bm)
        assert not result.valid
        assert any("Forbidden legacy source paths" in e for e in result.errors)

    def test_validate_existing_benchmark_no_regression(self) -> None:
        """Run validate_benchmark() on a real benchmark to ensure no false positives."""
        benchmark_path = (
            Path(__file__).parent.parent / "benchmarks" / "afc-curl-delta-01"
        )

        if not benchmark_path.exists():
            pytest.skip("Benchmark afc-curl-delta-01 not available")

        result = validate_benchmark(benchmark_path)
        assert result.valid, (
            f"Expected afc-curl-delta-01 to pass validation, "
            f"but got errors: {result.errors}"
        )

    def test_validate_tarball_naming_standalone_missing_pkgs(
        self, tmp_path: Path
    ) -> None:
        """validate_tarball_naming() returns error when pkgs/ is missing."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            has_pkgs=False,
        )

        result = validate_tarball_naming(bm)
        assert not result.valid
        assert any("pkgs/" in e for e in result.errors)


# ============================================================================
# No-regression: existing validation behavior preserved
# ============================================================================


class TestExistingValidationPreserved:
    """Verify existing validation checks (meta.yaml, project.yaml, etc.) still work."""

    def test_missing_dockerfile_still_errors(self, tmp_path: Path) -> None:
        """Missing Dockerfile is still an error."""
        bm = tmp_path / "bm"
        bm.mkdir()
        (bm / "project.yaml").write_text("main_repo: test\n")
        (bm / ".aixcc").mkdir()
        (bm / ".aixcc" / "meta.yaml").write_text("full_mode:\n  base_commit: abc123\n")

        result = validate_benchmark(bm)
        assert not result.valid
        assert "Missing Dockerfile" in result.errors

    def test_missing_meta_yaml_still_errors(self, tmp_path: Path) -> None:
        """Missing .aixcc/meta.yaml is still an error."""
        bm = tmp_path / "bm"
        bm.mkdir()
        (bm / "Dockerfile").write_text(VALID_DOCKERFILE)
        (bm / "project.yaml").write_text("main_repo: test\n")

        result = validate_benchmark(bm)
        assert not result.valid
        assert "Missing .aixcc/meta.yaml" in result.errors

    def test_pkg_refs_warning_still_works(self, tmp_path: Path) -> None:
        """Missing pkg_refs.txt still produces a warning (not error)."""
        bm = _create_benchmark(
            tmp_path,
            dockerfile_content=VALID_DOCKERFILE,
            tarball_name="foo.tar.gz",
            has_pkg_refs=False,
        )

        result = validate_benchmark(bm)
        assert result.valid  # Valid because it's just a warning
        assert any("pkg_refs.txt" in w for w in result.warnings)
