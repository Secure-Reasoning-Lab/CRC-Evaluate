"""Tests for CSV report generator."""

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
        "total_time": 3600.0,
        "time_to_first_pov": 120.5,
        "snapshot_count": 2,
        "time_series": [
            {
                "elapsed_time": 60.0,
                "cumulative_povs": 1,
                "cumulative_patches": 0,
                "llm_tokens": 200,
                "llm_cost": 0.25,
            },
            {
                "elapsed_time": 120.0,
                "cumulative_povs": 3,
                "cumulative_patches": 0,
                "llm_tokens": 500,
                "llm_cost": 0.60,
            },
        ],
    }


@pytest.fixture
def sample_experiment_metrics():
    """Sample experiment metrics for testing."""
    return {
        "trials": [
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
                "total_time": 3600.0,
                "time_to_first_pov": 120.5,
                "time_series": [
                    {
                        "elapsed_time": 60.0,
                        "cumulative_povs": 1,
                        "cumulative_patches": 0,
                        "llm_tokens": 200,
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
                "total_time": 1800.0,
                "time_to_first_pov": 90.0,
                "time_series": [
                    {
                        "elapsed_time": 90.0,
                        "cumulative_povs": 2,
                        "cumulative_patches": 0,
                        "llm_tokens": 600,
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
    assert "elapsed_time,cumulative_povs" in content
    assert "60.0,1" in content
    assert "120.0,3" in content


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
