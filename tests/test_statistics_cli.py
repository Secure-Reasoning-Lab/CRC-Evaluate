"""Tests for benchmark statistics CLI behavior."""

from __future__ import annotations

import argparse
import csv

import yaml
from crsbench.statistics.cli import run_stats


def _write_benchmark(
    benchmarks_dir,
    benchmark_name: str,
    *,
    language: str = "c",
    mode: str = "delta",
) -> None:
    benchmark_dir = benchmarks_dir / benchmark_name
    cpv_dir = benchmark_dir / ".aixcc" / "harness-one" / "cpv_0"
    (cpv_dir / "blobs").mkdir(parents=True)
    (cpv_dir / "patches").mkdir(parents=True)

    (benchmark_dir / "project.yaml").write_text(
        f"language: {language}\nmain_repo: https://example.com/repo.git\n"
    )
    (benchmark_dir / "test.sh").write_text("#!/bin/sh\n")
    (benchmark_dir / ".aixcc" / "meta.yaml").write_text(
        f"{mode}_mode: true\n"
        "harness_files:\n"
        "  - name: harness-one\n"
        "    vulns:\n"
        "      - vuln_keyword: cpv_0\n"
    )
    (cpv_dir / "vuln.yaml").write_text("id: cpv_0\ncwes:\n  - CWE-416\n")
    (cpv_dir / "blobs" / "pov_0.blob").write_text("blob\n")
    (cpv_dir / "patches" / "fix.diff").write_text("diff\n")


def test_run_stats_uses_benchmark_suite(tmp_path) -> None:
    benchmarks_dir = tmp_path / "benchmarks"
    suites_root = tmp_path / "benchmark-suites"
    output_path = tmp_path / "suite_stats.csv"

    _write_benchmark(benchmarks_dir, "afc-alpha-delta-01")
    _write_benchmark(benchmarks_dir, "afc-beta-delta-01")
    _write_benchmark(benchmarks_dir, "atlanta-gamma-full-01", mode="full")

    suites_root.mkdir()
    (suites_root / "selected.yaml").write_text(
        "Name: selected\n"
        "Description: Selected benchmarks\n"
        "Release date: 01.01.2026\n"
        "benchmark_list:\n"
        "  - afc-alpha-delta-01\n"
        "  - atlanta-gamma-full-01\n"
    )

    args = argparse.Namespace(
        benchmarks_dir=benchmarks_dir,
        filter=None,
        benchmarks=None,
        benchmark_suite="selected",
        benchmark_suites_root=suites_root,
        output=output_path,
        vuln_index_output=None,
        include_no_vulns=False,
        summary_only=False,
        verbose=False,
    )

    result = run_stats(args)

    assert result == 0
    assert output_path.exists()
    summary_path = (
        output_path.parent / f"{output_path.stem}_summary{output_path.suffix}"
    )
    assert summary_path.exists()

    with output_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert [row["Benchmark Name"] for row in rows] == [
        "afc-alpha-delta-01",
        "atlanta-gamma-full-01",
    ]


def test_run_stats_rejects_benchmarks_and_suite_together(tmp_path) -> None:
    args = argparse.Namespace(
        benchmarks_dir=tmp_path / "benchmarks",
        filter=None,
        benchmarks=["afc-alpha-delta-01"],
        benchmark_suite="selected",
        benchmark_suites_root=tmp_path / "benchmark-suites",
        output=tmp_path / "stats.csv",
        include_no_vulns=False,
        summary_only=True,
        verbose=False,
    )

    result = run_stats(args)

    assert result == 1


def test_run_stats_exports_vuln_index_yaml(tmp_path) -> None:
    benchmarks_dir = tmp_path / "benchmarks"
    vuln_index_output = tmp_path / "vuln-index.yaml"

    _write_benchmark(benchmarks_dir, "afc-alpha-delta-01")

    args = argparse.Namespace(
        benchmarks_dir=benchmarks_dir,
        filter=None,
        benchmarks=["afc-alpha-delta-01"],
        benchmark_suite=None,
        benchmark_suites_root=None,
        output=tmp_path / "stats.csv",
        vuln_index_output=vuln_index_output,
        include_no_vulns=False,
        summary_only=False,
        verbose=False,
    )

    result = run_stats(args)

    assert result == 0
    with vuln_index_output.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "benchmarks" in data
    assert data["benchmarks"]["afc-alpha-delta-01"]["harness-one"]["cpv_0"]["id"] == (
        "cpv_0"
    )
    assert data["benchmarks"]["afc-alpha-delta-01"]["harness-one"]["cpv_0"]["cwes"] == [
        "CWE-416"
    ]
