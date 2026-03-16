"""Tests for CI result checking functions."""

import json
from pathlib import Path

import yaml
from crsbench.benchmark_ci.checks import (
    check_coverage,
    check_patch_verify,
    check_verify,
)


class TestCheckVerify:
    """Tests for check_verify function."""

    def test_check_verify_all_cpvs_found(self, tmp_path: Path) -> None:
        """Test verify passes when all expected CPVs are found."""
        # Create benchmark with meta.yaml
        benchmark_path = tmp_path / "test-benchmark"
        aixcc_dir = benchmark_path / ".aixcc"
        aixcc_dir.mkdir(parents=True)

        meta = {
            "harness_files": [
                {
                    "name": "fuzz_target",
                    "vulns": [
                        {"vuln_keyword": "cpv_0"},
                        {"vuln_keyword": "cpv_1"},
                    ],
                }
            ]
        }
        (aixcc_dir / "meta.yaml").write_text(yaml.dump(meta))

        # Create results with all CPVs found
        results = [{"cpv_matched": ["cpv_0", "cpv_1"]}]
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(results))

        assert check_verify(benchmark_path, results_file) is True

    def test_check_verify_missing_cpvs(self, tmp_path: Path) -> None:
        """Test verify fails when some CPVs are missing."""
        benchmark_path = tmp_path / "test-benchmark"
        aixcc_dir = benchmark_path / ".aixcc"
        aixcc_dir.mkdir(parents=True)

        meta = {
            "harness_files": [
                {
                    "name": "fuzz_target",
                    "vulns": [
                        {"vuln_keyword": "cpv_0"},
                        {"vuln_keyword": "cpv_1"},
                    ],
                }
            ]
        }
        (aixcc_dir / "meta.yaml").write_text(yaml.dump(meta))

        # Create results with only one CPV found
        results = [{"cpv_matched": ["cpv_0"]}]
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(results))

        assert check_verify(benchmark_path, results_file) is False

    def test_check_verify_empty_results(self, tmp_path: Path) -> None:
        """Test verify fails with empty results."""
        benchmark_path = tmp_path / "test-benchmark"
        aixcc_dir = benchmark_path / ".aixcc"
        aixcc_dir.mkdir(parents=True)

        meta = {
            "harness_files": [
                {"name": "fuzz_target", "vulns": [{"vuln_keyword": "cpv_0"}]}
            ]
        }
        (aixcc_dir / "meta.yaml").write_text(yaml.dump(meta))

        results_file = tmp_path / "results.json"
        results_file.write_text("[]")

        assert check_verify(benchmark_path, results_file) is False


class TestCheckPatchVerify:
    """Tests for check_patch_verify function."""

    def test_check_patch_verify_all_pass(self, tmp_path: Path) -> None:
        """Test patch-verify passes when all patches pass."""
        results = [
            {"pov_id": "pov1", "patch_id": "patch1", "security_verdict": "PASS"},
            {"pov_id": "pov2", "patch_id": "patch2", "security_verdict": "PASS"},
        ]
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(results))

        assert check_patch_verify(results_file) is True

    def test_check_patch_verify_some_fail(self, tmp_path: Path) -> None:
        """Test patch-verify fails when some patches fail."""
        results = [
            {"pov_id": "pov1", "patch_id": "patch1", "security_verdict": "PASS"},
            {"pov_id": "pov2", "patch_id": "patch2", "security_verdict": "FAIL"},
        ]
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(results))

        assert check_patch_verify(results_file) is False

    def test_check_patch_verify_missing_pov_id(self, tmp_path: Path) -> None:
        """Test patch-verify fails when pov_id is missing."""
        results = [
            {"patch_id": "patch1", "security_verdict": "PASS"},
        ]
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(results))

        assert check_patch_verify(results_file) is False

    def test_check_patch_verify_dict_format(self, tmp_path: Path) -> None:
        """Test patch-verify works with dict format containing 'results' key."""
        data = {
            "results": [
                {"pov_id": "pov1", "patch_id": "patch1", "security_verdict": "PASS"},
            ]
        }
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_patch_verify(results_file) is True

    def test_check_patch_verify_empty_results(self, tmp_path: Path) -> None:
        """Test patch-verify fails with empty results."""
        results_file = tmp_path / "results.json"
        results_file.write_text("[]")

        assert check_patch_verify(results_file) is False


class TestCheckCoverage:
    """Tests for check_coverage function."""

    def test_check_coverage_valid(self, tmp_path: Path) -> None:
        """Test coverage passes with valid coverage data."""
        data = {
            "harness": "fuzz_target",
            "summary": {
                "lines_covered": 10,
                "lines_total": 100,
                "lines_percent": 10.0,
            },
        }
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_coverage(results_file) is True

    def test_check_coverage_zero_lines_covered(self, tmp_path: Path) -> None:
        """Test coverage fails when no lines are covered."""
        data = {
            "harness": "fuzz_target",
            "summary": {
                "lines_covered": 0,
                "lines_total": 100,
                "lines_percent": 0.0,
            },
        }
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_coverage(results_file) is False

    def test_check_coverage_zero_lines_total(self, tmp_path: Path) -> None:
        """Test coverage fails when no lines are found."""
        data = {
            "harness": "fuzz_target",
            "summary": {
                "lines_covered": 0,
                "lines_total": 0,
                "lines_percent": 0.0,
            },
        }
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_coverage(results_file) is False

    def test_check_coverage_unknown_total_with_covered_lines(
        self, tmp_path: Path
    ) -> None:
        """Unknown totals are acceptable when coverage still found lines."""
        data = {
            "harness": "fuzz_target",
            "summary": {
                "lines_covered": 7,
                "lines_total": 0,
                "lines_percent": 0.0,
            },
        }
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_coverage(results_file) is True

    def test_check_coverage_missing_summary(self, tmp_path: Path) -> None:
        """Test coverage fails when summary is missing."""
        data = {"harness": "fuzz_target"}
        results_file = tmp_path / "results.json"
        results_file.write_text(json.dumps(data))

        assert check_coverage(results_file) is False
