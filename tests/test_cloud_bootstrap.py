"""Unit tests for provider-neutral cloud VM bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

import crsbench.cloud.bootstrap as bootstrap_module
import pytest
from crsbench.cloud.bootstrap import (
    CloudBenchmarkSelector,
    CloudVmBootstrapInputs,
    prepare_command_args,
    run_benchmark_download,
    run_prepare,
    should_download_benchmarks,
)


def test_auto_skips_download_for_sanity_suite():
    inputs = CloudVmBootstrapInputs(benchmark_suite="sanity")

    assert should_download_benchmarks(inputs) is False


def test_auto_downloads_for_non_sanity_suite():
    inputs = CloudVmBootstrapInputs(benchmark_suite="afc-final")

    assert should_download_benchmarks(inputs) is True


def test_auto_downloads_for_explicit_benchmarks():
    inputs = CloudVmBootstrapInputs(benchmarks=["afc-demo-01"])

    assert should_download_benchmarks(inputs) is True


@pytest.mark.parametrize(
    ("download_benchmarks", "expected"),
    [("never", False), ("always", True)],
)
def test_explicit_download_policy_overrides_auto(
    download_benchmarks: str, expected: bool
):
    inputs = CloudVmBootstrapInputs(
        download_benchmarks=download_benchmarks,
        benchmark_suite="sanity",
    )

    assert should_download_benchmarks(inputs) is expected


def test_selector_shape_for_suite_backed_inputs_uses_suite_name():
    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(benchmark_suite="afc-final")
    )

    assert selector.benchmark_suite == "afc-final"
    assert selector.benchmarks is None
    assert selector.benchmarks_root is None
    assert selector.benchmark_suites_root is None


def test_selector_shape_for_explicit_benchmarks_preserves_raw_list():
    benchmarks = [
        "afc-demo-01",
        {"sanity-demo-02": ["fuzz_xml"]},
        {"asc-demo-03": {"fuzz_json": ["cpv_0"]}},
    ]

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(benchmarks=benchmarks)
    )

    assert selector.benchmark_suite is None
    assert selector.benchmarks == benchmarks
    assert selector.benchmarks_root is None
    assert selector.benchmark_suites_root is None


def test_selector_shape_omits_repo_default_roots():
    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmark_suite="afc-final",
            benchmarks_root=Path("benchmarks"),
            benchmark_suites_root=Path("benchmark-suites"),
        )
    )

    assert selector.benchmarks_root is None
    assert selector.benchmark_suites_root is None


def test_selector_shape_preserves_non_default_roots():
    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmark_suite="afc-final",
            benchmarks_root=Path("custom-benchmarks"),
            benchmark_suites_root=Path("/srv/custom-suites"),
        )
    )

    assert selector.benchmarks_root == Path("custom-benchmarks")
    assert selector.benchmark_suites_root == Path("/srv/custom-suites")


@pytest.mark.parametrize(
    ("prepare_mode", "expected"),
    [
        ("full", ["crsbench", "prepare"]),
        ("skip_base_images", ["crsbench", "prepare", "--skip-base-images"]),
    ],
)
def test_prepare_command_args(prepare_mode: str, expected: list[str]):
    assert prepare_command_args(prepare_mode) == expected


def test_run_prepare_invokes_prepare_command(monkeypatch):
    commands: list[tuple[list[str], bool]] = []

    def fake_run(cmd: list[str], *, check: bool) -> None:
        commands.append((cmd, check))

    monkeypatch.setattr(bootstrap_module.subprocess, "run", fake_run)

    run_prepare("skip_base_images")

    assert commands == [
        (["crsbench", "prepare", "--skip-base-images"], True),
    ]


def test_run_benchmark_download_uses_suite_download(monkeypatch):
    calls: list[tuple[str, Path, Path, bool]] = []

    def fake_download_suite(
        suite_name: str,
        output_dir: Path,
        suites_root: Path,
        *,
        no_ground_truth: bool = False,
    ) -> list[Path]:
        calls.append((suite_name, output_dir, suites_root, no_ground_truth))
        return [output_dir]

    monkeypatch.setattr(bootstrap_module, "download_suite", fake_download_suite)

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmark_suite="afc-final",
            benchmarks_root=Path("benchmarks-local"),
            benchmark_suites_root=Path("benchmark-suites-local"),
        )
    )

    result = run_benchmark_download(selector)

    assert calls == [
        (
            "afc-final",
            Path("benchmarks-local"),
            Path("benchmark-suites-local"),
            False,
        )
    ]
    assert result == [Path("benchmarks-local")]


def test_run_benchmark_download_uses_python_download_api_for_explicit_benchmarks(
    monkeypatch,
):
    calls: list[tuple[str, Path, list[str] | None, bool]] = []

    def fake_download_dataset(
        dataset: str,
        output_dir: Path,
        *,
        benchmarks: list[str] | None = None,
        no_ground_truth: bool = False,
    ) -> Path:
        calls.append((dataset, output_dir, benchmarks, no_ground_truth))
        return output_dir

    monkeypatch.setattr(bootstrap_module, "download_dataset", fake_download_dataset)

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmarks=[
                "afc-demo-01",
                {"sanity-demo-02": ["fuzz_xml"]},
                {"asc-demo-03": {"fuzz_json": ["cpv_0"]}},
            ],
            benchmarks_root=Path("/srv/benchmarks"),
        )
    )

    result = run_benchmark_download(selector)

    assert calls == [
        (
            "crsbench",
            Path("/srv/benchmarks"),
            ["afc-demo-01", "sanity-demo-02", "asc-demo-03"],
            False,
        )
    ]
    assert result == [Path("/srv/benchmarks")]
