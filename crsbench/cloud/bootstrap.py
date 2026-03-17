"""Shared bootstrap helpers for cloud-managed CRSBench VMs."""

from __future__ import annotations

import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from crsbench.dataset.download import download_dataset, download_suite
from crsbench.dataset.registry import resolve_prefix
from crsbench.validation.schemas import ExperimentConfig


@dataclass(frozen=True)
class CloudBenchmarkSelector:
    """Benchmark selector and roots needed by VM bootstrap."""

    benchmark_suite: str | None
    benchmarks: list[str] | None
    benchmarks_root: Path
    benchmark_suites_root: Path


@dataclass(frozen=True)
class CloudVmBootstrapInputs:
    """Provider-neutral bootstrap inputs for one cloud VM."""

    prepare_mode: str
    download_benchmarks: str
    selector: CloudBenchmarkSelector

    @classmethod
    def from_experiment_config(
        cls,
        config: ExperimentConfig,
    ) -> CloudVmBootstrapInputs:
        bootstrap = config.cloud.bootstrap if config.cloud is not None else None
        selector = CloudBenchmarkSelector(
            benchmark_suite=config.benchmark_suite,
            benchmarks=config.get_benchmark_list()
            if config.benchmarks is not None
            else None,
            benchmarks_root=Path(config.benchmarks_root),
            benchmark_suites_root=Path(config.benchmark_suites_root),
        )
        return cls(
            prepare_mode=bootstrap.prepare_mode if bootstrap is not None else "full",
            download_benchmarks=(
                bootstrap.download_benchmarks if bootstrap is not None else "auto"
            ),
            selector=selector,
        )


def should_download_benchmarks(inputs: CloudVmBootstrapInputs) -> bool:
    """Resolve the effective benchmark-download policy for a VM bootstrap."""
    policy = inputs.download_benchmarks
    if policy == "always":
        return True
    if policy == "never":
        return False
    if inputs.selector.benchmark_suite == "sanity":
        return False
    return True


def prepare_command_args(prepare_mode: str) -> list[str]:
    """Return the `crsbench prepare` command for the selected mode."""
    cmd = ["crsbench", "prepare"]
    if prepare_mode == "full":
        return cmd
    if prepare_mode == "skip_base_images":
        return [*cmd, "--skip-base-images"]
    raise ValueError(f"Unsupported prepare mode: {prepare_mode}")


def run_prepare(
    prepare_mode: str,
    *,
    cwd: Path,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    """Run `crsbench prepare` from a cloud VM checkout."""
    runner(prepare_command_args(prepare_mode), cwd=cwd, check=True)


def run_benchmark_download(
    inputs: CloudVmBootstrapInputs,
    *,
    download_suite_fn: Callable[..., list[Path]] = download_suite,
    download_dataset_fn: Callable[..., Path] = download_dataset,
) -> list[Path]:
    """Download benchmarks required by one cloud VM bootstrap."""
    selector = inputs.selector
    if selector.benchmark_suite:
        return download_suite_fn(
            selector.benchmark_suite,
            selector.benchmarks_root,
            selector.benchmark_suites_root,
            no_ground_truth=False,
        )
    if selector.benchmarks:
        results: list[Path] = []
        grouped = _group_benchmarks_by_dataset(selector.benchmarks)
        for dataset, names in grouped.items():
            results.append(
                download_dataset_fn(
                    dataset,
                    selector.benchmarks_root,
                    benchmarks=names,
                    no_ground_truth=False,
                )
            )
        return results
    raise ValueError("Cloud VM bootstrap requires a benchmark selector")


def run_cloud_vm_bootstrap(
    inputs: CloudVmBootstrapInputs,
    *,
    cwd: Path,
    runner: Callable[..., object] = subprocess.run,
    download_suite_fn: Callable[..., list[Path]] = download_suite,
    download_dataset_fn: Callable[..., Path] = download_dataset,
) -> list[Path]:
    """Run the shared prepare/download bootstrap sequence for a cloud VM."""
    run_prepare(inputs.prepare_mode, cwd=cwd, runner=runner)
    if not should_download_benchmarks(inputs):
        return []
    return run_benchmark_download(
        inputs,
        download_suite_fn=download_suite_fn,
        download_dataset_fn=download_dataset_fn,
    )


def _group_benchmarks_by_dataset(benchmarks: list[str]) -> OrderedDict[str, list[str]]:
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    for benchmark in benchmarks:
        dataset = resolve_prefix(benchmark)
        if dataset is None:
            raise ValueError(f"Unknown benchmark selector: {benchmark}")
        grouped.setdefault(dataset, []).append(benchmark)
    return grouped
