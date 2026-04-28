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
        "total_povs_discovered": 5,
        "unique_pov_names": ["pov_a", "pov_b", "pov_c"],
        "povs_cpv": 2,
        "povs_unintended": 1,
        "povs_not_vulnerable": 0,
        "povs_error": 0,
        "unintended_unique_sites": 1,
        "total_patches_generated": 0,
        "unique_patch_names": [],
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
                "total_povs_discovered": 5,
                "unique_pov_names": ["pov_a", "pov_b", "pov_c"],
                "povs_cpv": 2,
                "povs_unintended": 1,
                "povs_not_vulnerable": 0,
                "povs_error": 0,
                "unintended_unique_sites": 1,
                "total_patches_generated": 0,
                "unique_patch_names": [],
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
                "total_povs_discovered": 2,
                "unique_pov_names": ["pov_x", "pov_y"],
                "povs_cpv": 0,
                "povs_unintended": 2,
                "povs_not_vulnerable": 0,
                "povs_error": 0,
                "unintended_unique_sites": 2,
                "total_patches_generated": 0,
                "unique_patch_names": [],
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
    # Per-status breakdown surfaced from pov_store.json.
    assert row["povs_cpv"] == 2
    assert row["povs_unintended"] == 1
    assert row["povs_not_vulnerable"] == 0
    assert row["povs_error"] == 0
    assert row["unintended_unique_sites"] == 1
    assert row["total_llm_cost"] == 1.23
    assert row["snapshot_count"] == 2


def test_format_trial_row_breakdown_defaults_when_missing(temp_output_dir):
    """Trials with no pov_store.json yield zero counts, not KeyErrors."""
    generator = CSVReportGenerator(temp_output_dir)
    minimal = {
        "trial_dir": "experiment/x/y/full/address/trial-1",
        "trial_num": "trial-1",
        "crs": "x",
        "benchmark": "y",
        "harness": "h",
        "mode": "bug_finding",
        "total_povs_discovered": 0,
        "unique_pov_names": [],
        "total_patches_generated": 0,
        "unique_patch_names": [],
    }
    row = generator._format_trial_row(minimal)
    assert row["povs_cpv"] == 0
    assert row["povs_unintended"] == 0
    assert row["povs_not_vulnerable"] == 0
    assert row["povs_error"] == 0
    assert row["unintended_unique_sites"] == 0


def test_format_trial_row_consumes_trial_metrics_model_dump(temp_output_dir):
    """Regression: CSV row must read POV/patch counts from model_dump() keys.

    ``TrialMetrics.unique_povs``/``unique_patches`` are plain ``@property``,
    so Pydantic ``model_dump()`` does not include them. The CSV generator
    must derive counts from the actual fields (``total_povs_discovered``,
    ``unique_pov_names``, etc.) — not from the absent property keys, which
    previously caused ``total_povs``/``unique_povs`` columns to render as 0
    even when ``pov_names`` listed many entries.
    """
    from crsbench.reporting.models import TrialMetrics, TrialMode

    trial = TrialMetrics(
        trial_dir="experiment/json-c__ensemble-c/fuzz_json/full/address/trial-1",
        trial_num=1,
        crs="ensemble-c",
        benchmark="json-c",
        harness="fuzz_json",
        mode=TrialMode.bug_finding,
        total_povs_discovered=21,
        unique_pov_names=[f"pov_{i}" for i in range(21)],
        total_patches_generated=4,
        unique_patch_names=[f"patch_{i}" for i in range(4)],
    )
    dumped = trial.model_dump()
    assert "unique_povs" not in dumped
    assert "unique_patches" not in dumped

    generator = CSVReportGenerator(temp_output_dir)
    row = generator._format_trial_row(dumped)

    assert row["total_povs"] == 21
    assert row["unique_povs"] == 21
    assert row["total_patches"] == 4
    assert row["unique_patches"] == 4
    assert row["pov_names"].count(";") == 20
    assert row["patch_names"].count(";") == 3


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


def _write_cpv_trial(
    trial_dir: Path,
    *,
    trial_num: int,
    benchmark: str,
    harness: str,
    expected_cpv_ids: list[str],
    cpv_to_first_pov: dict,
    timestamp: str | float | None = None,
) -> None:
    """Helper to scaffold a trial dir with metadata + pov_store + history."""
    trial_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "trial_num": trial_num,
        "crs": "crs-bug-finding-claude-code",
        "benchmark": benchmark,
        "harness": harness,
        "mode": "bug_finding",
        "build_mode": "delta",
        "sanitizer": "address",
    }
    if timestamp is not None:
        metadata["timestamp"] = timestamp
    (trial_dir / "metadata.json").write_text(json.dumps(metadata))
    pov_dir = trial_dir / "povs"
    pov_dir.mkdir(parents=True, exist_ok=True)
    (pov_dir / "snapshot_history.json").write_text(
        json.dumps({"expected_cpv_ids": expected_cpv_ids})
    )
    (pov_dir / "pov_store.json").write_text(
        json.dumps({"cpv_to_first_pov": cpv_to_first_pov})
    )


def test_cpv_analysis_emits_row_per_trial_cpv(temp_output_dir):
    """One row per (trial, cpv) pair, matched flag reflects pov_store."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"

        # Trial with one expected CPV that was matched.
        _write_cpv_trial(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1",
            trial_num=1,
            benchmark="afc-x",
            harness="harness_a",
            expected_cpv_ids=["cpv_0"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "abc123",
                    "discovery_ts": 1000.0,
                    "relative_time": 42.5,
                }
            },
        )

        # Trial with two expected CPVs, one matched one not.
        _write_cpv_trial(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-y"
            / "harness_b"
            / "delta"
            / "address"
            / "trial-2",
            trial_num=2,
            benchmark="afc-y",
            harness="harness_b",
            expected_cpv_ids=["cpv_0", "cpv_1"],
            cpv_to_first_pov={
                "cpv_1": {
                    "pov_hash": "def456",
                    "discovery_ts": 2000.0,
                    "relative_time": 100.0,
                }
            },
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    by_key = {(r["benchmark"], r["cpv_id"]): r for r in rows}
    assert len(rows) == 3

    matched = by_key[("afc-x", "cpv_0")]
    assert matched["matched"] == "True"
    assert matched["time_to_trigger"] == "42.5"
    assert matched["pov_hash"] == "abc123"
    assert matched["trial_num"] == "1"

    unmatched = by_key[("afc-y", "cpv_0")]
    assert unmatched["matched"] == "False"
    assert unmatched["time_to_trigger"] == ""
    assert unmatched["pov_hash"] == ""

    matched_y = by_key[("afc-y", "cpv_1")]
    assert matched_y["matched"] == "True"
    assert matched_y["time_to_trigger"] == "100.0"


def test_cpv_analysis_recomputes_time_to_trigger_from_metadata_timestamp(
    temp_output_dir,
):
    """Do not trust pov_store.relative_time when crs_run_start_time drifted."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        _write_cpv_trial(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1",
            trial_num=1,
            benchmark="afc-x",
            harness="harness_a",
            expected_cpv_ids=["cpv_0"],
            timestamp="2026-04-26T08:00:00Z",
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "abc123",
                    "discovery_ts": 1777191900.0,
                    # Stale/corrupted value from a shifted crs_run_start_time.
                    "relative_time": -84900.0,
                }
            },
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["matched"] == "True"
    assert rows[0]["time_to_trigger"] == "1500.0"


def test_cpv_analysis_skips_trial_without_pov_store(temp_output_dir):
    """Without ``benchmarks_root``, trials missing both pov_store and
    expected_cpv_ids still produce no rows (legacy behavior preserved)."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "trial_num": 1,
                    "crs": "crs-bug-finding-claude-code",
                    "benchmark": "afc-x",
                    "harness": "harness_a",
                    "mode": "bug_finding",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert rows == []


def test_cpv_analysis_falls_back_to_meta_yaml_when_no_pov_store(temp_output_dir):
    """When ``benchmarks_root`` is supplied, a trial that died before the
    first snapshot (no ``povs/`` dir) still emits one row per expected CPV
    from meta.yaml, all marked ``matched=False``. Otherwise the trial
    silently disappears from the report and skews the denominator.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        experiment_dir = tmp / "exp"
        benchmarks_root = tmp / "benchmarks"

        # Synthesize a benchmark with two CPVs under sanitizer=address, plus
        # one CPV under sanitizer=memory that the trial should NOT see.
        benchmark_dir = benchmarks_root / "afc-x"
        (benchmark_dir / ".aixcc").mkdir(parents=True, exist_ok=True)
        (benchmark_dir / ".aixcc" / "meta.yaml").write_text(
            "delta_mode:\n"
            "  base_commit: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
            "  ref_commit: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'\n"
            "harness_files:\n"
            "- name: harness_a\n"
            "  path: $REPO/h.c\n"
            "  vulns:\n"
            "  - vuln_keyword: cpv_0\n"
            "    povs:\n"
            "    - id: pov_0\n"
            "      sanitizer: address\n"
            "      error_token: token0\n"
            "  - vuln_keyword: cpv_1\n"
            "    povs:\n"
            "    - id: pov_0\n"
            "      sanitizer: address\n"
            "      error_token: token1\n"
            "  - vuln_keyword: cpv_2\n"
            "    povs:\n"
            "    - id: pov_0\n"
            "      sanitizer: memory\n"
            "      error_token: token2\n"
        )
        (benchmark_dir / "project.yaml").write_text(
            "main_repo: 'git@example.com:x.git'\nlanguage: c\n"
        )

        # Trial died before first snapshot: only metadata.json exists.
        trial_dir = (
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "trial_num": 1,
                    "crs": "crs-bug-finding-claude-code",
                    "benchmark": "afc-x",
                    "harness": "harness_a",
                    "mode": "bug_finding",
                    "build_mode": "delta",
                    "sanitizer": "address",
                }
            )
        )

        generator = CSVReportGenerator(temp_output_dir, benchmarks_root)
        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    # Two CPVs under sanitizer=address; cpv_2 (memory) must be filtered out.
    assert {(r["cpv_id"], r["matched"]) for r in rows} == {
        ("cpv_0", "False"),
        ("cpv_1", "False"),
    }
    for row in rows:
        assert row["trial_num"] == "1"
        assert row["benchmark"] == "afc-x"
        assert row["harness"] == "harness_a"
        assert row["sanitizer"] == "address"
        assert row["time_to_trigger"] == ""
        assert row["pov_hash"] == ""
        assert row["discovery_ts"] == ""


def test_cpv_analysis_surfaces_unexpected_match(temp_output_dir):
    """A CPV present in pov_store but not in expected list still appears."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        _write_cpv_trial(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-z"
            / "harness_c"
            / "delta"
            / "address"
            / "trial-1",
            trial_num=1,
            benchmark="afc-z",
            harness="harness_c",
            expected_cpv_ids=["cpv_0"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1.0,
                    "relative_time": 10.0,
                },
                "cpv_extra": {
                    "pov_hash": "h1",
                    "discovery_ts": 2.0,
                    "relative_time": 20.0,
                },
            },
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    cpv_ids = sorted(r["cpv_id"] for r in rows)
    assert cpv_ids == ["cpv_0", "cpv_extra"]
    extra = next(r for r in rows if r["cpv_id"] == "cpv_extra")
    assert extra["matched"] == "True"
    assert extra["time_to_trigger"] == "20.0"


def _write_cpv_trial_from_povs(
    trial_dir: Path,
    *,
    trial_num: int,
    benchmark: str,
    harness: str,
    expected_cpv_ids: list[str],
    crs_run_start_time: float,
    povs: dict[str, dict],
) -> None:
    """Helper to scaffold a trial whose pov_store has empty cpv_to_first_pov.

    Mirrors the reanalysis-rewritten layout where matches live only inside
    ``povs[hash].cpv_matched`` and the denormalized top-level map is empty.
    """
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "metadata.json").write_text(
        json.dumps(
            {
                "trial_num": trial_num,
                "crs": "crs-bug-finding-claude-code",
                "benchmark": benchmark,
                "harness": harness,
                "mode": "bug_finding",
                "build_mode": "delta",
                "sanitizer": "address",
            }
        )
    )
    pov_dir = trial_dir / "povs"
    pov_dir.mkdir(parents=True, exist_ok=True)
    (pov_dir / "snapshot_history.json").write_text(
        json.dumps({"expected_cpv_ids": expected_cpv_ids})
    )
    (pov_dir / "pov_store.json").write_text(
        json.dumps(
            {
                "crs_run_start_time": crs_run_start_time,
                "povs": povs,
                "cpv_to_first_pov": {},
            }
        )
    )


def test_cpv_analysis_derives_first_pov_from_povs_when_map_empty(temp_output_dir):
    """When pov_store top-level cpv_to_first_pov is empty (e.g. after
    reanalysis rewrite), derive matches from ``povs[].cpv_matched``."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        _write_cpv_trial_from_povs(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "atlanta-q"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1",
            trial_num=1,
            benchmark="atlanta-q",
            harness="harness_a",
            expected_cpv_ids=["cpv_0", "cpv_1"],
            crs_run_start_time=1000.0,
            povs={
                # cpv_0: two povs, earlier file_mtime should win
                "h_late": {
                    "cpv_matched": ["cpv_0"],
                    "file_mtime": 1200.0,
                    "first_seen_ts": 1205.0,
                    "status": "cpv",
                },
                "h_early": {
                    "cpv_matched": ["cpv_0", "cpv_1"],
                    "file_mtime": 1050.0,
                    "first_seen_ts": 1060.0,
                    "status": "cpv",
                },
                # POVs without cpv_matched should be ignored
                "h_unintended": {
                    "cpv_matched": [],
                    "file_mtime": 1100.0,
                    "first_seen_ts": 1100.0,
                    "status": "unintended",
                },
            },
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    by_cpv = {r["cpv_id"]: r for r in rows}
    assert set(by_cpv) == {"cpv_0", "cpv_1"}
    # cpv_0 picks the earlier mtime POV (h_early)
    assert by_cpv["cpv_0"]["matched"] == "True"
    assert by_cpv["cpv_0"]["pov_hash"] == "h_early"
    assert by_cpv["cpv_0"]["time_to_trigger"] == "50.0"
    # cpv_1 only matched by h_early
    assert by_cpv["cpv_1"]["matched"] == "True"
    assert by_cpv["cpv_1"]["pov_hash"] == "h_early"


def test_cpv_analysis_falls_back_to_first_seen_ts_when_mtime_missing(
    temp_output_dir,
):
    """If file_mtime is absent, derivation uses first_seen_ts."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        _write_cpv_trial_from_povs(
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "atlanta-r"
            / "harness_b"
            / "delta"
            / "address"
            / "trial-1",
            trial_num=1,
            benchmark="atlanta-r",
            harness="harness_b",
            expected_cpv_ids=["cpv_0"],
            crs_run_start_time=1000.0,
            povs={
                "h_only": {
                    "cpv_matched": ["cpv_0"],
                    "file_mtime": None,
                    "first_seen_ts": 1075.0,
                    "status": "cpv",
                },
            },
        )

        out_path = generator.generate_cpv_analysis_report(experiment_dir)
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["matched"] == "True"
    assert rows[0]["time_to_trigger"] == "75.0"


# ---------------------------------------------------------------------------
# Budget-cutoff variant of cpv_analysis
# ---------------------------------------------------------------------------


def _write_llm_usage(trial_dir: Path, total_cost_usd: float) -> None:
    (trial_dir / "llm-usage.json").write_text(
        json.dumps({"total_cost_usd": total_cost_usd})
    )


def test_compute_time_at_budget_full_run_within_budget():
    """Total cost ≤ budget → +inf so async-drain matches stay preserved."""
    ts = [
        {"running_elapsed_time": 100.0, "llm_cost": 1.0},
        {"running_elapsed_time": 200.0, "llm_cost": 2.0},
    ]
    t = CSVReportGenerator._compute_time_at_budget(
        ts, total_cost_usd=2.0, budget_usd=5.0
    )
    assert t == float("inf")


def test_compute_time_at_budget_last_sample_fits_but_total_exceeds():
    """Last sample ≤ budget but total > budget → fall back to last_t."""
    ts = [
        {"running_elapsed_time": 100.0, "llm_cost": 1.0},
        {"running_elapsed_time": 200.0, "llm_cost": 4.0},
    ]
    t = CSVReportGenerator._compute_time_at_budget(
        ts, total_cost_usd=7.0, budget_usd=5.0
    )
    assert t == 200.0


def test_match_after_last_snapshot_preserved_when_trial_within_budget(
    temp_output_dir,
):
    """A POV verified after the final snapshot still counts when the trial
    finished within budget (async-drain edge case observed in
    finding_all_cc-finding-original/atlanta-faad2 trial-2)."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir / "crs" / "afc-z" / "h" / "delta" / "address" / "trial-1"
        )
        _write_cpv_trial(
            trial_dir,
            trial_num=1,
            benchmark="afc-z",
            harness="h",
            expected_cpv_ids=["cpv_0"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1.0,
                    "relative_time": 13996.0,
                }
            },
        )
        _write_llm_usage(trial_dir, total_cost_usd=30.22)

        # Last snapshot at 10827s, well before the POV's relative_time.
        time_series = {
            str(trial_dir): [
                {"running_elapsed_time": 0.0, "llm_cost": 0.0},
                {"running_elapsed_time": 5000.0, "llm_cost": 15.0},
                {"running_elapsed_time": 10827.0, "llm_cost": 28.0},
            ]
        }

        out_path = generator.generate_cpv_analysis_report(
            experiment_dir, budget_usd=50.0, trial_time_series=time_series
        )
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["matched"] == "True"
    assert rows[0]["time_to_trigger"] == "13996.0"


def test_compute_time_at_budget_linear_interpolation():
    """Budget falls between two samples → interpolate run-elapsed time."""
    ts = [
        {"running_elapsed_time": 0.0, "llm_cost": 0.0},
        {"running_elapsed_time": 100.0, "llm_cost": 2.0},
        {"running_elapsed_time": 200.0, "llm_cost": 6.0},
    ]
    # Budget $4 falls halfway between $2 (t=100) and $6 (t=200) → t=150.
    t = CSVReportGenerator._compute_time_at_budget(
        ts, total_cost_usd=6.0, budget_usd=4.0
    )
    assert t == pytest.approx(150.0)


def test_compute_time_at_budget_first_sample_already_over():
    """First sample exceeds budget → interpolate from origin (0,0)."""
    ts = [
        {"running_elapsed_time": 1000.0, "llm_cost": 10.0},
    ]
    # Budget $2.5 → 25% of the way from (0,0) to (1000, 10).
    t = CSVReportGenerator._compute_time_at_budget(
        ts, total_cost_usd=10.0, budget_usd=2.5
    )
    assert t == pytest.approx(250.0)


def test_compute_time_at_budget_no_snapshot_falls_back_to_total_cost():
    """No time_series but total_cost ≤ budget → treat as fully within budget."""
    t = CSVReportGenerator._compute_time_at_budget(
        [], total_cost_usd=0.5, budget_usd=5.0
    )
    assert t == float("inf")


def test_compute_time_at_budget_no_data_returns_none():
    """No time_series and unknown total cost → None (preserve original match)."""
    t = CSVReportGenerator._compute_time_at_budget(
        [], total_cost_usd=None, budget_usd=5.0
    )
    assert t is None


def test_cpv_analysis_budget_filters_late_discoveries(temp_output_dir):
    """CPVs discovered after the budget runs out are marked unmatched."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1"
        )
        # cpv_0 found at 50s (early); cpv_1 found at 1500s (late).
        _write_cpv_trial(
            trial_dir,
            trial_num=1,
            benchmark="afc-x",
            harness="harness_a",
            expected_cpv_ids=["cpv_0", "cpv_1"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1.0,
                    "relative_time": 50.0,
                },
                "cpv_1": {
                    "pov_hash": "h1",
                    "discovery_ts": 2.0,
                    "relative_time": 1500.0,
                },
            },
        )
        _write_llm_usage(trial_dir, total_cost_usd=20.0)

        # Cost timeline: $0 at t=0, $5 at t=600, $20 at t=2400.
        # Budget $5 → t_b = 600s. cpv_0 (50s) within; cpv_1 (1500s) outside.
        time_series = {
            str(trial_dir): [
                {"running_elapsed_time": 0.0, "llm_cost": 0.0},
                {"running_elapsed_time": 600.0, "llm_cost": 5.0},
                {"running_elapsed_time": 2400.0, "llm_cost": 20.0},
            ]
        }

        out_path = generator.generate_cpv_analysis_report(
            experiment_dir, budget_usd=5.0, trial_time_series=time_series
        )

        assert out_path.name == "cpv_analysis_budget_5.csv"
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    by_cpv = {r["cpv_id"]: r for r in rows}
    assert by_cpv["cpv_0"]["matched"] == "True"
    assert by_cpv["cpv_0"]["time_to_trigger"] == "50.0"
    assert by_cpv["cpv_0"]["pov_hash"] == "h0"
    assert by_cpv["cpv_0"]["budget_usd"] == "5.0"
    assert by_cpv["cpv_0"]["trial_total_cost_usd"] == "20.0"
    assert by_cpv["cpv_0"]["trial_time_at_budget"] == "600.0"

    assert by_cpv["cpv_1"]["matched"] == "False"
    assert by_cpv["cpv_1"]["time_to_trigger"] == ""
    assert by_cpv["cpv_1"]["pov_hash"] == ""


def test_cpv_analysis_budget_uses_metadata_recomputed_trigger_time(
    temp_output_dir,
):
    """Budget filtering uses the same metadata-anchored CPV time as CSV output."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-x"
            / "harness_a"
            / "delta"
            / "address"
            / "trial-1"
        )
        _write_cpv_trial(
            trial_dir,
            trial_num=1,
            benchmark="afc-x",
            harness="harness_a",
            expected_cpv_ids=["cpv_0"],
            timestamp="2026-04-26T08:00:00+00:00",
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1777191900.0,
                    "relative_time": -84900.0,
                },
            },
        )
        _write_llm_usage(trial_dir, total_cost_usd=20.0)
        time_series = {
            str(trial_dir): [
                {"running_elapsed_time": 0.0, "llm_cost": 0.0},
                {"running_elapsed_time": 1000.0, "llm_cost": 5.0},
                {"running_elapsed_time": 2000.0, "llm_cost": 10.0},
            ]
        }

        out_path = generator.generate_cpv_analysis_report(
            experiment_dir, budget_usd=5.0, trial_time_series=time_series
        )
        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["matched"] == "False"
    assert rows[0]["time_to_trigger"] == ""
    assert rows[0]["discovery_ts"] == ""


def test_cpv_analysis_budget_keeps_match_when_total_cost_below_budget(
    temp_output_dir,
):
    """Trials whose total LLM spend is below the budget keep all matches."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir
            / "crs-bug-finding-claude-code"
            / "afc-y"
            / "harness_b"
            / "delta"
            / "address"
            / "trial-1"
        )
        _write_cpv_trial(
            trial_dir,
            trial_num=1,
            benchmark="afc-y",
            harness="harness_b",
            expected_cpv_ids=["cpv_0"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1.0,
                    "relative_time": 800.0,
                }
            },
        )
        # Trial finished cheap (under $1), no time-series sampled.
        _write_llm_usage(trial_dir, total_cost_usd=0.5)

        out_path = generator.generate_cpv_analysis_report(
            experiment_dir, budget_usd=5.0, trial_time_series={}
        )

        with out_path.open(newline="") as f:
            rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["matched"] == "True"
    assert rows[0]["time_to_trigger"] == "800.0"
    assert rows[0]["budget_usd"] == "5.0"
    assert rows[0]["trial_total_cost_usd"] == "0.5"


def test_cpv_analysis_budget_filename_decimal(temp_output_dir):
    """Decimal budgets render with stripped trailing zeros (e.g. 7.5)."""
    generator = CSVReportGenerator(temp_output_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        experiment_dir = Path(tmpdir) / "exp"
        trial_dir = (
            experiment_dir / "crs" / "afc-x" / "h" / "delta" / "address" / "trial-1"
        )
        _write_cpv_trial(
            trial_dir,
            trial_num=1,
            benchmark="afc-x",
            harness="h",
            expected_cpv_ids=["cpv_0"],
            cpv_to_first_pov={
                "cpv_0": {
                    "pov_hash": "h0",
                    "discovery_ts": 1.0,
                    "relative_time": 5.0,
                }
            },
        )
        _write_llm_usage(trial_dir, total_cost_usd=0.0)

        out_path = generator.generate_cpv_analysis_report(
            experiment_dir, budget_usd=7.5, trial_time_series={}
        )
        assert out_path.name == "cpv_analysis_budget_7.5.csv"
