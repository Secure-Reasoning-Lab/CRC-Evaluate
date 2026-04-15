"""Tests for CSV report generator."""

import csv
import json
import tempfile
from pathlib import Path

import pytest
from crsbench.reporting.generators.csv import CSVReportGenerator


@pytest.fixture
def temp_output_dir():
    """Create a temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_trial_metrics():
    """Sample trial metrics for testing."""
    return {
        "trial_dir": "experiment/json-c__ensemble-c/fuzz_json/full/address/trial-1",
        "trial_num": "trial-1",
        "crs": "ensemble-c",
        "benchmark": "json-c",
        "harness": "fuzz_json",
        "mode": "bug_finding",
        "run_mode": "full",
        "sanitizer": "address",
        "total_povs": 5,
        "unique_povs": 3,
        "total_patches": 0,
        "unique_patches": 0,
        "total_llm_cost": 1.23,
        "total_llm_tokens": 1000,
        "total_llm_input_tokens": 750,
        "total_llm_output_tokens": 250,
        "total_time": 3600.0,
        "time_to_first_pov": 120.5,
        "snapshot_count": 2,
        "time_series": [
            {
                "elapsed_time": 60.0,
                "running_elapsed_time": 0.0,
                "cumulative_povs": 1,
                "cumulative_patches": 0,
                "llm_tokens": 200,
                "llm_input_tokens": 150,
                "llm_output_tokens": 50,
                "llm_cost": 0.25,
            },
            {
                "elapsed_time": 120.0,
                "running_elapsed_time": 110.0,
                "cumulative_povs": 3,
                "cumulative_patches": 0,
                "llm_tokens": 500,
                "llm_input_tokens": 375,
                "llm_output_tokens": 125,
                "llm_cost": 0.60,
            },
        ],
    }


@pytest.fixture
def sample_experiment_metrics():
    """Sample experiment metrics for testing."""
    return {
        "trial_metrics": [
            {
                "trial_dir": "experiment/json-c__ensemble-c/fuzz_json/full/address/trial-1",
                "trial_num": "trial-1",
                "crs": "ensemble-c",
                "benchmark": "json-c",
                "harness": "fuzz_json",
                "mode": "bug_finding",
                "run_mode": "full",
                "sanitizer": "address",
                "total_povs": 5,
                "unique_povs": 3,
                "total_patches": 0,
                "unique_patches": 0,
                "total_llm_cost": 1.23,
                "total_llm_tokens": 1000,
                "total_llm_input_tokens": 750,
                "total_llm_output_tokens": 250,
                "total_time": 3600.0,
                "time_to_first_pov": 120.5,
                "time_series": [
                    {
                        "elapsed_time": 60.0,
                        "running_elapsed_time": 55.0,
                        "cumulative_povs": 1,
                        "cumulative_patches": 0,
                        "llm_tokens": 200,
                        "llm_input_tokens": 150,
                        "llm_output_tokens": 50,
                        "llm_cost": 0.25,
                    }
                ],
            },
            {
                "trial_dir": "experiment/libxml2__atlantis-c/fuzz_xml/delta/undefined/trial-2",
                "trial_num": "trial-2",
                "crs": "atlantis-c",
                "benchmark": "libxml2",
                "harness": "fuzz_xml",
                "mode": "bug_finding",
                "run_mode": "delta",
                "sanitizer": "undefined",
                "total_povs": 2,
                "unique_povs": 2,
                "total_patches": 0,
                "unique_patches": 0,
                "total_llm_cost": 0.80,
                "total_llm_tokens": 600,
                "total_llm_input_tokens": 450,
                "total_llm_output_tokens": 150,
                "total_time": 1800.0,
                "time_to_first_pov": 90.0,
                "time_series": [
                    {
                        "elapsed_time": 90.0,
                        "running_elapsed_time": 85.0,
                        "cumulative_povs": 2,
                        "cumulative_patches": 0,
                        "llm_tokens": 600,
                        "llm_input_tokens": 450,
                        "llm_output_tokens": 150,
                        "llm_cost": 0.80,
                    }
                ],
            },
        ],
        "by_crs": {
            "ensemble-c": {
                "trial_count": 1,
                "avg_povs": 5.0,
                "avg_patches": 0.0,
                "avg_cost": 1.23,
                "total_cost": 1.23,
                "total_povs": 5,
            },
            "atlantis-c": {
                "trial_count": 1,
                "avg_povs": 2.0,
                "avg_patches": 0.0,
                "avg_cost": 0.80,
                "total_cost": 0.80,
                "total_povs": 2,
            },
        },
        "by_benchmark": {
            "json-c": {
                "trial_count": 1,
                "avg_povs": 5.0,
                "avg_patches": 0.0,
                "avg_time_to_first_pov": 120.5,
                "total_cost": 1.23,
            },
            "libxml2": {
                "trial_count": 1,
                "avg_povs": 2.0,
                "avg_patches": 0.0,
                "avg_time_to_first_pov": 90.0,
                "total_cost": 0.80,
            },
        },
    }


def test_csv_generator_init(temp_output_dir):
    """Test CSV generator initialization."""
    generator = CSVReportGenerator(temp_output_dir)
    assert generator.output_dir == temp_output_dir
    assert temp_output_dir.exists()


def test_generate_trial_report(temp_output_dir, sample_trial_metrics):
    """Test generating CSV report for a single trial."""
    generator = CSVReportGenerator(temp_output_dir)
    # No need to pass snapshots - uses time_series from trial_metrics
    output_files = generator.generate_trial_report(sample_trial_metrics)

    assert len(output_files) == 2

    # Check for unique trial ID in filenames (in trial-reports subdirectory)
    trial_reports_dir = temp_output_dir / "trial-reports"
    assert trial_reports_dir.exists()

    # Path: experiment/json-c__ensemble-c/fuzz_json/full/address/trial-1
    # Skip first part: json-c__ensemble-c-fuzz_json-full-address-trial-1
    trial_id = "json-c__ensemble-c-fuzz_json-full-address-trial-1"
    assert (trial_reports_dir / f"{trial_id}_summary.csv").exists()
    assert (trial_reports_dir / f"{trial_id}_time_series.csv").exists()

    # Verify trial summary content
    trial_csv = trial_reports_dir / f"{trial_id}_summary.csv"
    content = trial_csv.read_text()
    assert "trial_num,crs,benchmark" in content
    assert "trial-1,ensemble-c,json-c" in content

    # Verify time series content
    time_series_csv = trial_reports_dir / f"{trial_id}_time_series.csv"
    content = time_series_csv.read_text()
    assert "elapsed_time" in content
    assert "running_elapsed_time" in content
    assert "cumulative_povs" in content
    assert "60.0,0.0,1" in content
    assert "120.0,110.0,3" in content


def test_generate_experiment_report(temp_output_dir, sample_experiment_metrics):
    """Test generating CSV reports for an experiment."""
    generator = CSVReportGenerator(temp_output_dir)
    output_files = generator.generate_experiment_report(sample_experiment_metrics)

    assert len(output_files) == 5
    assert (temp_output_dir / "trial_summary.csv").exists()
    assert (temp_output_dir / "crs_summary.csv").exists()
    assert (temp_output_dir / "benchmark_summary.csv").exists()
    assert (temp_output_dir / "time_series.csv").exists()
    assert (temp_output_dir / "combined_report.csv").exists()

    # Verify trial summary has 2 rows
    trial_csv = temp_output_dir / "trial_summary.csv"
    lines = trial_csv.read_text().strip().split("\n")
    assert len(lines) == 3  # header + 2 trials

    # Verify CRS summary has 2 rows
    crs_csv = temp_output_dir / "crs_summary.csv"
    lines = crs_csv.read_text().strip().split("\n")
    assert len(lines) == 3  # header + 2 CRS

    # Verify benchmark summary has 2 rows
    benchmark_csv = temp_output_dir / "benchmark_summary.csv"
    lines = benchmark_csv.read_text().strip().split("\n")
    assert len(lines) == 3  # header + 2 benchmarks

    # Verify time series has 2 rows (one snapshot per trial)
    time_series_csv = temp_output_dir / "time_series.csv"
    lines = time_series_csv.read_text().strip().split("\n")
    assert len(lines) == 3  # header + 2 snapshots

    # Verify combined report
    combined_csv = temp_output_dir / "combined_report.csv"
    content = combined_csv.read_text()
    assert "record_type" in content
    assert "trial" in content
    assert "crs" in content
    assert "benchmark" in content
    assert "time_series" in content


def test_format_trial_row(temp_output_dir, sample_trial_metrics):
    """Test formatting trial metrics into CSV row."""
    generator = CSVReportGenerator(temp_output_dir)
    row = generator._format_trial_row(sample_trial_metrics)

    assert row["trial_num"] == "trial-1"
    assert row["crs"] == "ensemble-c"
    assert row["benchmark"] == "json-c"
    assert row["harness"] == "fuzz_json"
    assert row["mode"] == "bug_finding"
    assert row["total_povs"] == 5
    assert row["unique_povs"] == 3
    assert row["total_llm_cost"] == 1.23
    assert row["snapshot_count"] == 2


def test_format_crs_row(temp_output_dir):
    """Test formatting CRS summary into CSV row."""
    generator = CSVReportGenerator(temp_output_dir)
    crs_data = {
        "trial_count": 3,
        "avg_povs": 4.5,
        "avg_patches": 2.0,
        "avg_cost": 1.50,
        "total_cost": 4.50,
        "total_povs": 15,
    }
    row = generator._format_crs_row("ensemble-c", crs_data)

    assert row["crs"] == "ensemble-c"
    assert row["trial_count"] == 3
    assert row["avg_povs"] == 4.5
    assert row["total_cost"] == 4.50


def test_format_benchmark_row(temp_output_dir):
    """Test formatting benchmark summary into CSV row."""
    generator = CSVReportGenerator(temp_output_dir)
    bench_data = {
        "trial_count": 2,
        "avg_povs": 3.5,
        "avg_patches": 1.0,
        "avg_time_to_first_pov": 105.0,
        "total_cost": 2.50,
    }
    row = generator._format_benchmark_row("json-c", bench_data)

    assert row["benchmark"] == "json-c"
    assert row["trial_count"] == 2
    assert row["avg_povs"] == 3.5
    assert row["avg_time_to_first_pov"] == 105.0


def test_format_time_series_row(temp_output_dir, sample_trial_metrics):
    """Test formatting time series point into CSV row."""
    generator = CSVReportGenerator(temp_output_dir)
    ts_point = sample_trial_metrics["time_series"][0]
    row = generator._format_time_series_row(sample_trial_metrics, ts_point)

    assert row["trial_num"] == "trial-1"
    assert row["crs"] == "ensemble-c"
    assert row["benchmark"] == "json-c"
    assert row["harness"] == "fuzz_json"
    assert row["mode"] == "bug_finding"
    assert row["run_mode"] == "full"
    assert row["sanitizer"] == "address"
    assert row["elapsed_time"] == 60.0
    assert row["running_elapsed_time"] == 0.0
    assert row["cumulative_povs"] == 1
    assert row["llm_tokens"] == 200


def test_combined_report_structure(temp_output_dir, sample_experiment_metrics):
    """Test combined report has correct structure with record_type."""
    generator = CSVReportGenerator(temp_output_dir)
    generator.generate_experiment_report(sample_experiment_metrics)

    combined_csv = temp_output_dir / "combined_report.csv"
    lines = combined_csv.read_text().strip().split("\n")

    # Header + 2 trials + 2 CRS + 2 benchmarks + 2 time_series = 9 rows
    assert len(lines) == 9

    # Verify record types are present
    content = combined_csv.read_text()
    assert content.count("trial") >= 2
    assert content.count("crs") >= 2
    assert content.count("benchmark") >= 2
    assert content.count("time_series") >= 2


def test_generate_patch_analysis_report_includes_sidecar_api_calls(temp_output_dir):
    """Patch analysis includes sidecar POST API call counts."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "crs-codex"
            / "afc-shadowsocks-full-01"
            / "json_fuzz"
            / "cpv_0"
            / "full"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)

        (trial_dir / ".success").write_text("")
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "crs-codex",
                    "benchmark": "afc-shadowsocks-full-01",
                    "harness": "json_fuzz",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "full",
                    "sanitizer": "address",
                }
            )
        )

        services_dir = trial_dir / "output" / "logs" / "services"
        services_dir.mkdir(parents=True, exist_ok=True)
        (services_dir / "crs-codex_inc-builder-asan.stdout.log").write_text(
            "\n".join(
                [
                    'INFO: 127.0.0.1:11111 - "GET /health HTTP/1.1" 200 OK',
                    'INFO: 127.0.0.1:11111 - "POST /build HTTP/1.1" 200 OK',
                    'INFO: 127.0.0.1:11111 - "POST /run-pov HTTP/1.1" 200 OK',
                    'INFO: 127.0.0.1:11111 - "POST /run-test HTTP/1.1" 200 OK',
                ]
            )
        )

        out_path = generator.generate_patch_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert "builder_sidecar_api_calls" in rows[0]
    assert rows[0]["builder_sidecar_api_calls"] == "3"


def test_patch_analysis_sidecar_api_calls_fallback_to_crs_logs(temp_output_dir):
    """Patch analysis falls back to output/logs/crs when services logs are absent."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "crs-codex"
            / "afc-shadowsocks-full-01"
            / "json_fuzz"
            / "cpv_1"
            / "full"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)

        (trial_dir / ".success").write_text("")
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "crs-codex",
                    "benchmark": "afc-shadowsocks-full-01",
                    "harness": "json_fuzz",
                    "target_cpv_id": "cpv_1",
                    "build_mode": "full",
                    "sanitizer": "address",
                }
            )
        )

        crs_logs_dir = trial_dir / "output" / "logs" / "crs" / "crs-codex"
        crs_logs_dir.mkdir(parents=True, exist_ok=True)
        (crs_logs_dir / "crs-codex_inc-builder-asan.stdout.log").write_text(
            "\n".join(
                [
                    'INFO: 127.0.0.1:11111 - "GET /status/abc HTTP/1.1" 200 OK',
                    'INFO: 127.0.0.1:11111 - "POST /build HTTP/1.1" 200 OK',
                ]
            )
        )

        out_path = generator.generate_patch_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["builder_sidecar_api_calls"] == "1"


def test_patch_analysis_uses_max_generated_count(temp_output_dir):
    """Patch generated count should not undercount when summary is stale."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "crs-codex"
            / "afc-xz-full-01"
            / "fuzz_encode_stream"
            / "cpv_0"
            / "full"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)

        (trial_dir / ".success").write_text("")
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "crs-codex",
                    "benchmark": "afc-xz-full-01",
                    "harness": "fuzz_encode_stream",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "full",
                    "sanitizer": "address",
                }
            )
        )
        (trial_dir / "patch_verification_results.json").write_text(
            json.dumps(
                {
                    "summary": {"patches_generated": 1, "valid": 1},
                    "results": [
                        {
                            "patch_id": "patch_0",
                            "pov_id": "cpv_0",
                            "pov_test_passed": True,
                            "unit_tests_passed": True,
                        }
                    ],
                }
            )
        )

        output_patches = trial_dir / "output" / "patches" / "cpv_0"
        output_patches.mkdir(parents=True, exist_ok=True)
        (output_patches / "patch_0.diff").write_text("diff 0")
        (output_patches / "patch_1.diff").write_text("diff 1")

        out_path = generator.generate_patch_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["patch_generated_count"] == "2"
    assert rows[0]["verified_total_count"] == "1"


def test_patch_analysis_ignores_hidden_patch_diff_files(temp_output_dir):
    """Hidden patch diff files should not inflate generated patch count."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "crs-codex"
            / "afc-xz-full-01"
            / "fuzz_encode_stream"
            / "cpv_0"
            / "full"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)

        (trial_dir / ".success").write_text("")
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "crs-codex",
                    "benchmark": "afc-xz-full-01",
                    "harness": "fuzz_encode_stream",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "full",
                    "sanitizer": "address",
                }
            )
        )

        output_patches = trial_dir / "output" / "patches" / "cpv_0"
        output_patches.mkdir(parents=True, exist_ok=True)
        (output_patches / "patch_0.diff").write_text("diff 0")
        hidden_dir = trial_dir / "output" / "patches" / ".tmp"
        hidden_dir.mkdir(parents=True, exist_ok=True)
        (hidden_dir / "patch_hidden.diff").write_text("diff hidden")

        out_path = generator.generate_patch_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["patch_generated_count"] == "1"


def test_ci_test_report_marks_completed_no_patch_trials_as_skip(temp_output_dir):
    """Completed trials without generated patches should not look like log loss."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "builder-sidecar-lite"
            / "afc-apache-commons-compress-delta-01"
            / "CompressTarFuzzer"
            / "cpv_0"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "builder-sidecar-lite",
                    "benchmark": "afc-apache-commons-compress-delta-01",
                    "harness": "CompressTarFuzzer",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )
        (trial_dir / "worker.log").write_text(
            "\n".join(
                [
                    "No patches found for distributed verification",
                    "No patches directory found (CRS produced no patches): output/patches",
                    "[Trial 1] Completed builder-sidecar-lite on benchmark/harness: "
                    "0 patches produced, 0 verified, 0 valid in 147.6s",
                ]
            )
        )

        out_path = generator.generate_ci_test_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["ci_status"] == "SKIP"
    assert rows[0]["failure_reason"] == "no patches produced"


def test_ci_test_report_parses_verify_patch_success_as_pass(temp_output_dir):
    """builder-sidecar verify-patch success logs should be reported as PASS."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "builder-sidecar-lite"
            / "afc-curl-delta-01"
            / "curl_fuzzer_ws"
            / "cpv_0"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "builder-sidecar-lite",
                    "benchmark": "afc-curl-delta-01",
                    "harness": "curl_fuzzer_ws",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )
        (trial_dir / "worker.log").write_text(
            "[Trial 1] Completed builder-sidecar-lite on "
            "afc-curl-delta-01/curl_fuzzer_ws: 0 patches produced\n"
        )
        crs_log_dir = trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite"
        crs_log_dir.mkdir(parents=True, exist_ok=True)
        timing_dir = crs_log_dir / "log_dir"
        timing_dir.mkdir(parents=True, exist_ok=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps({"rebuild": 70.4, "test": 481.0})
        )
        (crs_log_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "\n".join(
                [
                    "builder-sidecar-lite_patcher-1  | 2026-04-10T05:42:35Z "
                    "[verify-patch] PASS: Functionality tests pass",
                    "builder-sidecar-lite_patcher-1  | 2026-04-10T05:42:35Z "
                    "[verify-patch]   [test] 481.0s",
                    "builder-sidecar-lite_patcher-1  | 2026-04-10T05:42:35Z "
                    "[verify-patch] SUCCESS: patch verified - crash fixed, tests pass",
                ]
            )
        )

        out_path = generator.generate_ci_test_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["ci_status"] == "PASS"
    assert rows[0]["failure_reason"] == ""
    assert rows[0]["patch_test_time"] == "481.0"
    assert rows[0]["test_time_s"] == "481.0"


def test_ci_test_report_prefers_structured_verify_patch_status(temp_output_dir):
    """Structured verify_patch_timing.json ``status`` field is authoritative.

    When the file contains a ``status`` field (new sidecar schema), the
    reporter should trust it and skip the legacy text parsing of patcher
    logs — even if those logs would otherwise look like "PASS".
    """
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "builder-sidecar-lite"
            / "atlanta-binutils-delta-01"
            / "fuzz_as"
            / "cpv_0"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "builder-sidecar-lite",
                    "benchmark": "atlanta-binutils-delta-01",
                    "harness": "fuzz_as",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )
        (trial_dir / "worker.log").write_text(
            "[Trial 1] Completed builder-sidecar-lite on "
            "atlanta-binutils-delta-01/fuzz_as: 0 patches produced\n"
        )
        crs_log_dir = trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite"
        crs_log_dir.mkdir(parents=True, exist_ok=True)
        timing_dir = crs_log_dir / "log_dir"
        timing_dir.mkdir(parents=True, exist_ok=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "Some POVs still crash after patch (1/1)",
                    "failed_step": "run_povs_patched",
                    "steps": {
                        "run_povs_patched": {
                            "status": "fail",
                            "still_crash": [
                                {
                                    "pov": "cpv_0",
                                    "exit": 1,
                                    "stderr_tail": "500 Server Error",
                                }
                            ],
                        }
                    },
                    "rebuild": 145.4,
                    "test": 0.0,
                }
            )
        )
        # A misleading "SUCCESS" line in the patcher log must be ignored
        # when the structured result is present.
        (crs_log_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "builder-sidecar-lite_patcher-1  | 2026-04-10T06:10:00Z "
            "[verify-patch] SUCCESS: patch verified - crash fixed, tests pass"
        )

        out_path = generator.generate_ci_test_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["ci_status"] == "FAIL"
    assert rows[0]["failure_reason"] == "Some POVs still crash after patch (1/1)"
    assert "run_povs_patched" in rows[0]["failure_log"]
    # Legacy rebuild timing should still be parsed from the top-level keys.
    assert rows[0]["patch_rebuild_time"] == "145.4"


def test_ci_test_report_structured_build_failure_classified(temp_output_dir):
    """A verify_patch_timing.json with failed_step=apply_patch_build yields BUILD_FAILED."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "experiment-data" / "exp-1"
        trial_dir = (
            experiment_dir
            / "builder-sidecar-lite"
            / "atlanta-htmlunit-delta-01"
            / "HtmlunitOne"
            / "cpv_0"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "crs": "builder-sidecar-lite",
                    "benchmark": "atlanta-htmlunit-delta-01",
                    "harness": "HtmlunitOne",
                    "target_cpv_id": "cpv_0",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )
        (trial_dir / "worker.log").write_text(
            "[Trial 1] Completed builder-sidecar-lite\n"
        )
        crs_log_dir = trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite"
        crs_log_dir.mkdir(parents=True, exist_ok=True)
        timing_dir = crs_log_dir / "log_dir"
        timing_dir.mkdir(parents=True, exist_ok=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "Patched build failed (exit=1)",
                    "failed_step": "apply_patch_build",
                    "steps": {
                        "apply_patch_build": {
                            "status": "fail",
                            "exit_code": 1,
                            "stderr_tail": "mv: cannot move 'org' ...",
                        }
                    },
                }
            )
        )

        out_path = generator.generate_ci_test_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["ci_status"] == "BUILD_FAILED"
    assert rows[0]["failure_reason"] == "Patched build failed (exit=1)"
