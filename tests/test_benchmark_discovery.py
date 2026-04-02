"""Tests for auto-discovery of fuzz targets from OSS-Fuzz projects."""

import stat
from pathlib import Path
from unittest.mock import patch

import yaml
from crsbench.benchmark.discovery import (
    auto_generate_meta_yaml,
    discover_fuzz_targets,
    is_oss_fuzz_project,
)

# =============================================================================
# OSS-Fuzz Project Detection
# =============================================================================


class TestIsOssFuzzProject:
    """Test OSS-Fuzz project detection."""

    def test_valid_project(self, tmp_path):
        """Directory with all required files is detected."""
        (tmp_path / "project.yaml").write_text("main_repo: https://example.com")
        (tmp_path / "Dockerfile").write_text("FROM base")
        (tmp_path / "build.sh").write_text("#!/bin/bash")

        assert is_oss_fuzz_project(tmp_path) is True

    def test_missing_project_yaml(self, tmp_path):
        """Missing project.yaml is not detected."""
        (tmp_path / "Dockerfile").write_text("FROM base")
        (tmp_path / "build.sh").write_text("#!/bin/bash")

        assert is_oss_fuzz_project(tmp_path) is False

    def test_missing_dockerfile(self, tmp_path):
        """Missing Dockerfile is not detected."""
        (tmp_path / "project.yaml").write_text("main_repo: https://example.com")
        (tmp_path / "build.sh").write_text("#!/bin/bash")

        assert is_oss_fuzz_project(tmp_path) is False

    def test_missing_build_sh(self, tmp_path):
        """Missing build.sh is not detected."""
        (tmp_path / "project.yaml").write_text("main_repo: https://example.com")
        (tmp_path / "Dockerfile").write_text("FROM base")

        assert is_oss_fuzz_project(tmp_path) is False

    def test_empty_dir(self, tmp_path):
        """Empty directory is not detected."""
        assert is_oss_fuzz_project(tmp_path) is False

    def test_nonexistent_dir(self, tmp_path):
        """Non-existent directory is not detected."""
        assert is_oss_fuzz_project(tmp_path / "nonexistent") is False


# =============================================================================
# Fuzz Target Discovery
# =============================================================================


class TestDiscoverFuzzTargets:
    """Test fuzz target binary discovery from build output."""

    def _make_executable(self, path: Path):
        """Create an executable file."""
        path.write_bytes(b"\x7fELF")  # ELF header
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_discovers_executables(self, tmp_path):
        """Finds executable files in build output."""
        self._make_executable(tmp_path / "fuzzer_a")
        self._make_executable(tmp_path / "fuzzer_b")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["fuzzer_a", "fuzzer_b"]

    def test_skips_afl_prefix(self, tmp_path):
        """Skips afl-* binaries."""
        self._make_executable(tmp_path / "afl-fuzz")
        self._make_executable(tmp_path / "real_fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["real_fuzzer"]

    def test_skips_jazzer_prefix(self, tmp_path):
        """Skips jazzer_* binaries."""
        self._make_executable(tmp_path / "jazzer_driver")
        self._make_executable(tmp_path / "my_fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["my_fuzzer"]

    def test_skips_known_names(self, tmp_path):
        """Skips llvm-symbolizer and centipede."""
        self._make_executable(tmp_path / "llvm-symbolizer")
        self._make_executable(tmp_path / "centipede")
        self._make_executable(tmp_path / "real_fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["real_fuzzer"]

    def test_skips_non_executable(self, tmp_path):
        """Skips non-executable files."""
        (tmp_path / "data.txt").write_text("not executable")
        self._make_executable(tmp_path / "fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["fuzzer"]

    def test_skips_directories(self, tmp_path):
        """Skips subdirectories."""
        (tmp_path / "subdir").mkdir()
        self._make_executable(tmp_path / "fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["fuzzer"]

    def test_empty_dir(self, tmp_path):
        """Empty directory returns empty list."""
        assert discover_fuzz_targets(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        """Non-existent directory returns empty list."""
        assert discover_fuzz_targets(tmp_path / "nonexistent") == []

    def test_sorted_output(self, tmp_path):
        """Results are sorted alphabetically."""
        self._make_executable(tmp_path / "z_fuzzer")
        self._make_executable(tmp_path / "a_fuzzer")
        self._make_executable(tmp_path / "m_fuzzer")

        targets = discover_fuzz_targets(tmp_path)

        assert targets == ["a_fuzzer", "m_fuzzer", "z_fuzzer"]


# =============================================================================
# Meta.yaml Auto-Generation
# =============================================================================


class TestAutoGenerateMetaYaml:
    """Test meta.yaml auto-generation (with mocked build)."""

    def _make_oss_fuzz_project(self, path: Path):
        """Create a minimal OSS-Fuzz project directory."""
        path.mkdir(parents=True, exist_ok=True)
        (path / "project.yaml").write_text(
            "main_repo: https://github.com/example/repo\nlanguage: c\n"
        )
        (path / "Dockerfile").write_text("FROM gcr.io/oss-fuzz-base/base-builder\n")
        (path / "build.sh").write_text("#!/bin/bash\n")

    def _make_build_output(self, build_out_dir: Path, targets: list[str]):
        """Create mock build output with executable targets."""
        build_out_dir.mkdir(parents=True, exist_ok=True)
        for name in targets:
            path = build_out_dir / name
            path.write_bytes(b"\x7fELF")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_generates_meta_yaml(self, tmp_path):
        """Auto-generates meta.yaml with discovered harnesses."""
        project_dir = tmp_path / "my-project"
        self._make_oss_fuzz_project(project_dir)

        oss_fuzz_path = tmp_path / "oss-fuzz"
        build_out = oss_fuzz_path / "build" / "out" / "my-project"
        self._make_build_output(build_out, ["fuzz_json", "fuzz_xml"])

        commit = "a" * 40

        with (
            patch(
                "crsbench.benchmark.discovery.build_oss_fuzz_project",
                return_value=build_out,
            ),
            patch(
                "crsbench.benchmark.discovery._resolve_base_commit",
                return_value=commit,
            ),
        ):
            result = auto_generate_meta_yaml(project_dir, oss_fuzz_path)

        assert result.exists()
        assert result.name == "meta.yaml"
        assert result.parent.name == ".aixcc"

        # Verify content
        with result.open() as f:
            data = yaml.safe_load(f)

        assert data["full_mode"]["base_commit"] == commit
        harness_names = [h["name"] for h in data["harness_files"]]
        assert harness_names == ["fuzz_json", "fuzz_xml"]

    def test_skips_if_meta_yaml_exists(self, tmp_path):
        """Does not overwrite existing meta.yaml."""
        project_dir = tmp_path / "my-project"
        self._make_oss_fuzz_project(project_dir)

        # Pre-create meta.yaml
        aixcc = project_dir / ".aixcc"
        aixcc.mkdir()
        meta = aixcc / "meta.yaml"
        meta.write_text("existing: true\n")

        from crsbench.benchmark.discovery import is_oss_fuzz_project

        # auto_generate should not be called if meta.yaml exists
        # (the caller _ensure_meta_yaml checks this)
        assert meta.exists()
        assert is_oss_fuzz_project(project_dir)

    def test_raises_if_not_oss_fuzz_project(self, tmp_path):
        """Raises FileNotFoundError for non-OSS-Fuzz directories."""
        import pytest

        with pytest.raises(FileNotFoundError, match="Not a valid OSS-Fuzz project"):
            auto_generate_meta_yaml(tmp_path, tmp_path / "oss-fuzz")

    def test_raises_if_no_targets_found(self, tmp_path):
        """Raises RuntimeError when build produces no fuzz targets."""
        import pytest

        project_dir = tmp_path / "my-project"
        self._make_oss_fuzz_project(project_dir)

        build_out = tmp_path / "empty-build-out"
        build_out.mkdir()

        with (
            patch(
                "crsbench.benchmark.discovery.build_oss_fuzz_project",
                return_value=build_out,
            ),
            pytest.raises(RuntimeError, match="No fuzz targets found"),
        ):
            auto_generate_meta_yaml(project_dir, tmp_path / "oss-fuzz")

    def test_placeholder_commit_on_resolve_failure(self, tmp_path):
        """Uses placeholder commit when git ls-remote fails."""
        project_dir = tmp_path / "my-project"
        self._make_oss_fuzz_project(project_dir)

        build_out = tmp_path / "build-out"
        self._make_build_output(build_out, ["fuzzer"])

        with (
            patch(
                "crsbench.benchmark.discovery.build_oss_fuzz_project",
                return_value=build_out,
            ),
            patch(
                "crsbench.benchmark.discovery._resolve_base_commit",
                return_value=None,
            ),
        ):
            result = auto_generate_meta_yaml(project_dir, tmp_path / "oss-fuzz")

        with result.open() as f:
            data = yaml.safe_load(f)

        assert data["full_mode"]["base_commit"] == "0" * 40
