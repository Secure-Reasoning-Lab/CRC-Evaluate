"""Tests for coverage-related infrastructure methods.

Tests:
- has_harness(): Check if harness exists in build output
- run_coverage(): Run coverage with --coverage-output-dir
- MetaYamlAdapter.from_benchmark_path(): Load adapter from benchmark dir
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.validation.meta_adapter import MetaYamlAdapter


@pytest.fixture
def mock_oss_fuzz(tmp_path: Path) -> Path:
    """Create mock oss-fuzz directory with coverage helper."""
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "projects").mkdir()
    (oss_fuzz / "build" / "out").mkdir(parents=True)

    # Mock helper.py that validates --coverage-output-dir
    helper = oss_fuzz / "infra" / "helper.py"
    helper.write_text('''#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="cmd")
cov = sub.add_parser("coverage")
cov.add_argument("--coverage-output-dir", required=True)
cov.add_argument("--corpus-dir", required=True)
cov.add_argument("--fuzz-target", required=True)
cov.add_argument("--no-serve", action="store_true")
cov.add_argument("project")
args = parser.parse_args()

if args.cmd == "coverage":
    out = Path(args.coverage_output_dir) / "report" / "linux"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({"data": [{"totals": {}}]}))
    sys.exit(0)
sys.exit(1)
''')
    return oss_fuzz


class TestHasHarness:
    """Tests for has_harness()."""

    def test_exists_returns_true(self, mock_oss_fuzz: Path):
        """Harness file exists → True."""
        build = mock_oss_fuzz / "build" / "out" / "proj"
        build.mkdir(parents=True)
        (build / "fuzz").write_text("binary")

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        assert infra.has_harness("proj", "fuzz") is True

    def test_missing_returns_false(self, mock_oss_fuzz: Path):
        """Harness doesn't exist → False."""
        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        assert infra.has_harness("proj", "fuzz") is False


class TestRunCoverage:
    """Tests for run_coverage()."""

    def test_passes_coverage_output_dir(self, mock_oss_fuzz: Path, tmp_path: Path):
        """Verify --coverage-output-dir is passed to helper.py."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "input").write_bytes(b"test")
        output = tmp_path / "out"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            infra = OSSFuzzInfrastructure(mock_oss_fuzz)
            infra.run_coverage("proj", "fuzz", corpus, output)

            cmd = mock_run.call_args[0][0]
            assert "--coverage-output-dir" in cmd
            assert str(output) in cmd


class TestMetaYamlAdapterFromBenchmarkPath:
    """Tests for MetaYamlAdapter.from_benchmark_path()."""

    def test_valid_benchmark(self, tmp_path: Path):
        """Load valid benchmark with meta.yaml and project.yaml."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()
        (bench / ".aixcc" / "meta.yaml").write_text("""
delta_mode:
  ref_commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  base_commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
harness_files:
  - name: fuzz
    path: /src/fuzz.c
    vulns:
      - vuln_keyword: cpv_0
        povs: [{id: pov_0, name: pov_0, path: cpv_0/pov_0, sanitizer: address}]
""")
        (bench / "project.yaml").write_text("language: c\nmain_repo: https://test.com")

        adapter = MetaYamlAdapter.from_benchmark_path(bench)
        assert adapter is not None
        assert adapter.benchmark_name == "test-bench"
        assert adapter.lang == "c"

    def test_missing_meta_returns_none(self, tmp_path: Path):
        """Missing meta.yaml → None."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()

        assert MetaYamlAdapter.from_benchmark_path(bench) is None

    def test_missing_project_yaml_uses_defaults(self, tmp_path: Path):
        """Missing project.yaml → use defaults (lang=c)."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()
        (bench / ".aixcc" / "meta.yaml").write_text("""
delta_mode:
  ref_commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  base_commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
harness_files:
  - name: fuzz
    path: /src/fuzz.c
    vulns:
      - vuln_keyword: cpv_0
        povs: [{id: pov_0, name: pov_0, path: cpv_0/pov_0, sanitizer: address}]
""")

        adapter = MetaYamlAdapter.from_benchmark_path(bench)
        assert adapter is not None
        assert adapter.lang == "c"
        assert adapter.main_repo == ""
