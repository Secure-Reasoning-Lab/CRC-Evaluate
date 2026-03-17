"""Shared bootstrap helpers for cloud-managed CRSBench VMs."""

from __future__ import annotations

import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from crsbench.dataset.download import download_dataset, download_suite
from crsbench.dataset.registry import resolve_prefix
from crsbench.validation.schemas import (
    ExperimentConfig,
    _normalize_benchmark_selector_list,
)

PrepareMode = Literal["full", "skip_base_images"]
DownloadBenchmarksMode = Literal["auto", "always", "never"]
BenchmarkSelectorValue = list[str] | dict[str, list[str]]
BenchmarkSelectorInput = str | dict[str, BenchmarkSelectorValue]
BenchmarkSelectorList = list[BenchmarkSelectorInput]

DEFAULT_BENCHMARKS_ROOT = Path("benchmarks")
DEFAULT_BENCHMARK_SUITES_ROOT = Path("benchmark-suites")


@dataclass(frozen=True)
class CloudBenchmarkSelector:
    """Benchmark selector and roots needed by VM bootstrap."""

    benchmark_suite: str | None = None
    benchmarks: BenchmarkSelectorList | None = None
    benchmarks_root: Path | None = None
    benchmark_suites_root: Path | None = None

    @classmethod
    def from_inputs(
        cls,
        inputs: CloudVmBootstrapInputs,
    ) -> CloudBenchmarkSelector:
        """Normalize raw bootstrap inputs into one benchmark selector contract."""
        benchmark_suite = _normalize_optional_string(inputs.benchmark_suite)
        benchmarks = _normalize_benchmark_selector_list(inputs.benchmarks, "benchmarks")
        if benchmark_suite is not None and benchmarks is not None:
            raise ValueError(
                "Cloud VM bootstrap inputs cannot define both benchmark_suite and benchmarks"
            )
        if benchmark_suite is None and benchmarks is None:
            raise ValueError(
                "Cloud VM bootstrap inputs require benchmark_suite or benchmarks"
            )
        return cls(
            benchmark_suite=benchmark_suite,
            benchmarks=benchmarks,
            benchmarks_root=_normalize_override_path(
                inputs.benchmarks_root,
                default_path=DEFAULT_BENCHMARKS_ROOT,
            ),
            benchmark_suites_root=(
                _normalize_override_path(
                    inputs.benchmark_suites_root,
                    default_path=DEFAULT_BENCHMARK_SUITES_ROOT,
                )
                if benchmark_suite is not None
                else None
            ),
        )

    def effective_benchmarks_root(self) -> Path:
        """Return the root path used by VM-side benchmark download."""
        return self.benchmarks_root or DEFAULT_BENCHMARKS_ROOT

    def effective_benchmark_suites_root(self) -> Path:
        """Return the suite root path used by suite-backed downloads."""
        return self.benchmark_suites_root or DEFAULT_BENCHMARK_SUITES_ROOT

    def benchmark_names(self) -> list[str]:
        """Extract benchmark names from raw benchmark selectors."""
        if self.benchmarks is None:
            return []

        names: list[str] = []
        for benchmark in self.benchmarks:
            if isinstance(benchmark, str):
                names.append(benchmark)
                continue
            names.append(next(iter(benchmark)))
        return names


@dataclass(frozen=True)
class CloudVmBootstrapInputs:
    """Provider-neutral bootstrap inputs for one cloud VM."""

    prepare_mode: PrepareMode = "full"
    download_benchmarks: DownloadBenchmarksMode = "auto"
    benchmark_suite: str | None = None
    benchmarks: BenchmarkSelectorList | None = None
    benchmarks_root: Path | str = DEFAULT_BENCHMARKS_ROOT
    benchmark_suites_root: Path | str = DEFAULT_BENCHMARK_SUITES_ROOT

    @property
    def selector(self) -> CloudBenchmarkSelector:
        """Return the normalized selector for these raw bootstrap inputs."""
        return CloudBenchmarkSelector.from_inputs(self)

    @classmethod
    def from_experiment_config(
        cls,
        config: ExperimentConfig,
    ) -> CloudVmBootstrapInputs:
        bootstrap = config.cloud.bootstrap if config.cloud is not None else None
        return cls(
            prepare_mode=bootstrap.prepare_mode if bootstrap is not None else "full",
            download_benchmarks=(
                bootstrap.download_benchmarks if bootstrap is not None else "auto"
            ),
            benchmark_suite=config.benchmark_suite,
            benchmarks=config.benchmarks,
            benchmarks_root=Path(config.benchmarks_root),
            benchmark_suites_root=Path(config.benchmark_suites_root),
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


def prepare_command_args(prepare_mode: PrepareMode) -> list[str]:
    """Return the `crsbench prepare` command for the selected mode."""
    cmd = ["crsbench", "prepare"]
    if prepare_mode == "full":
        return cmd
    if prepare_mode == "skip_base_images":
        return [*cmd, "--skip-base-images"]
    raise ValueError(f"Unsupported prepare mode: {prepare_mode}")


def run_prepare(
    prepare_mode: PrepareMode,
    *,
    cwd: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> None:
    """Run `crsbench prepare` from a cloud VM checkout."""
    cmd = prepare_command_args(prepare_mode)
    if runner is None:
        if cwd is None:
            subprocess.run(cmd, check=True)
            return
        subprocess.run(cmd, cwd=cwd, check=True)
        return
    if cwd is None:
        runner(cmd, check=True)
        return
    runner(cmd, cwd=cwd, check=True)


def run_benchmark_download(
    selector: CloudBenchmarkSelector,
    *,
    download_suite_fn: Callable[..., list[Path]] | None = None,
    download_dataset_fn: Callable[..., Path] | None = None,
) -> list[Path]:
    """Download benchmarks required by one cloud VM bootstrap."""
    selected_download_suite = (
        download_suite if download_suite_fn is None else download_suite_fn
    )
    selected_download_dataset = (
        download_dataset if download_dataset_fn is None else download_dataset_fn
    )
    if selector.benchmark_suite:
        return selected_download_suite(
            selector.benchmark_suite,
            selector.effective_benchmarks_root(),
            selector.effective_benchmark_suites_root(),
            no_ground_truth=False,
        )
    if selector.benchmarks:
        results: list[Path] = []
        grouped = _group_benchmarks_by_dataset(selector.benchmark_names())
        for dataset, names in grouped.items():
            results.append(
                selected_download_dataset(
                    dataset,
                    selector.effective_benchmarks_root(),
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
    runner: Callable[..., object] | None = None,
    download_suite_fn: Callable[..., list[Path]] | None = None,
    download_dataset_fn: Callable[..., Path] | None = None,
) -> list[Path]:
    """Run the shared prepare/download bootstrap sequence for a cloud VM."""
    run_prepare(inputs.prepare_mode, cwd=cwd, runner=runner)
    if not should_download_benchmarks(inputs):
        return []
    return run_benchmark_download(
        inputs.selector,
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


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_override_path(
    value: Path | str,
    *,
    default_path: Path,
) -> Path | None:
    path = Path(value)
    if path == default_path:
        return None
    return path
