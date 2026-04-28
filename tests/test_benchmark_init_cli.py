"""Tests for `crsbench benchmark init` parallelism."""

from __future__ import annotations

import argparse
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from crsbench.benchmark.packaging.cli.benchmark_command import (
    add_benchmark_subparser,
    handle_init,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeConfig(SimpleNamespace):
    def get_benchmark_list(self) -> list[str]:
        return list(self.benchmark_names)


def _make_init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    add_benchmark_subparser(subs)
    return parser


def _make_init_fixture(
    tmp_path: Path, benchmark_names: list[str]
) -> tuple[Path, _FakeConfig]:
    benchmarks_root = tmp_path / "benchmarks"
    benchmarks_root.mkdir()
    (tmp_path / "config.yaml").write_text("experiment: test\n")
    for name in benchmark_names:
        (benchmarks_root / name).mkdir()

    config = _FakeConfig(
        benchmarks_root=benchmarks_root,
        oss_fuzz_path=tmp_path / "oss-fuzz",
        benchmark_names=benchmark_names,
        build_timeout=1800,
        trials=1,
        sanitizers=["address"],
    )
    return benchmarks_root, config


def _write_generated_meta(benchmark_path: Path) -> Path:
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
    meta_yaml.parent.mkdir(exist_ok=True)
    meta_yaml.write_text("harness_files:\n  - name: fuzz_target\n")
    return meta_yaml


def test_benchmark_init_parser_accepts_jobs() -> None:
    parser = _make_init_parser()

    args = parser.parse_args(
        [
            "benchmark",
            "init",
            "--experiment-config",
            "config.yaml",
            "--jobs",
            "3",
            "--cpuset-cpus",
            "0-7",
        ]
    )

    assert args.jobs == 3
    assert args.cpuset_cpus == "0-7"


def test_handle_init_single_job_preserves_explicit_cpuset(tmp_path: Path) -> None:
    benchmarks_root, config = _make_init_fixture(tmp_path, ["gpac"])
    calls: list[tuple[str | None, int]] = []

    def fake_auto_generate_meta_yaml(
        benchmark_path: Path,
        oss_fuzz_path: Path,
        sanitizer: str = "address",
        *,
        cpuset_cpus: str | None = None,
        build_timeout: int,
    ) -> Path:
        del oss_fuzz_path, sanitizer
        calls.append((cpuset_cpus, build_timeout))
        return _write_generated_meta(benchmark_path)

    args = argparse.Namespace(
        experiment_config=str(tmp_path / "config.yaml"),
        cpuset_cpus="0-7",
        jobs=1,
    )

    with (
        patch(
            "crsbench.run_experiment.load_experiment_config",
            return_value=config,
        ),
        patch(
            "crsbench.evaluation.trial_paths.resolve_benchmarks_root",
            return_value=benchmarks_root,
        ),
        patch(
            "crsbench.benchmark.discovery.is_oss_fuzz_project",
            return_value=True,
        ),
        patch(
            "crsbench.benchmark.discovery.auto_generate_meta_yaml",
            side_effect=fake_auto_generate_meta_yaml,
        ),
    ):
        result = handle_init(args)

    assert result == 0
    assert calls == [("0-7", 1800)]


def test_handle_init_parallel_jobs_split_cpuset_across_builds(tmp_path: Path) -> None:
    benchmarks_root, config = _make_init_fixture(tmp_path, ["gpac", "mpv", "upx"])
    calls: list[tuple[str, str | None, int]] = []
    calls_lock = threading.Lock()
    overlap_ready = threading.Event()

    def fake_auto_generate_meta_yaml(
        benchmark_path: Path,
        oss_fuzz_path: Path,
        sanitizer: str = "address",
        *,
        cpuset_cpus: str | None = None,
        build_timeout: int,
    ) -> Path:
        del oss_fuzz_path, sanitizer
        with calls_lock:
            calls.append((benchmark_path.name, cpuset_cpus, build_timeout))
            if len(calls) == 2:
                overlap_ready.set()
            should_wait = len(calls) < 2
        if should_wait:
            assert overlap_ready.wait(timeout=2)
        return _write_generated_meta(benchmark_path)

    args = argparse.Namespace(
        experiment_config=str(tmp_path / "config.yaml"),
        cpuset_cpus="0-7",
        jobs=2,
    )

    with (
        patch(
            "crsbench.run_experiment.load_experiment_config",
            return_value=config,
        ),
        patch(
            "crsbench.evaluation.trial_paths.resolve_benchmarks_root",
            return_value=benchmarks_root,
        ),
        patch(
            "crsbench.benchmark.discovery.is_oss_fuzz_project",
            return_value=True,
        ),
        patch(
            "crsbench.benchmark.discovery.auto_generate_meta_yaml",
            side_effect=fake_auto_generate_meta_yaml,
        ),
    ):
        result = handle_init(args)

    assert result == 0
    assert len(calls) == 3
    assert {calls[0][1], calls[1][1]} == {"0-3", "4-7"}
    assert calls[2][1] in {"0-3", "4-7"}
    assert {call[2] for call in calls} == {1800}
