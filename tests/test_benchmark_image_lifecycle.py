import argparse
from pathlib import Path
from unittest.mock import Mock, patch

from crsbench.benchmark.packaging.cli.benchmark_command import (
    handle_image_build,
    handle_image_prepare,
)


def _make_inc_benchmark(root: Path, name: str) -> None:
    bench = root / name
    (bench / ".aixcc").mkdir(parents=True)
    (bench / "project.yaml").write_text(
        "inc_build: true\nsanitizers:\n  - address\n",
        encoding="utf-8",
    )


def _build_args(benchmarks_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        benchmarks_dir=str(benchmarks_dir),
        filter=None,
        workers=1,
        local_prefix="crsbench",
        registry="ghcr.io/team-atlanta/crsbench",
        policy="auto",
        max_pull_bytes=None,
        pull_timeout=300,
    )


def test_image_prepare_creates_project_alias_before_ensure(tmp_path: Path) -> None:
    benchmarks_dir = tmp_path / "benchmarks"
    benchmarks_dir.mkdir()
    benchmark_name = "afc-curl-delta-01"
    _make_inc_benchmark(benchmarks_dir, benchmark_name)
    args = _build_args(benchmarks_dir)

    infra = Mock()
    infra.ensure_inc_image.return_value = True

    with (
        patch(
            "crsbench.builder.infrastructure.OSSFuzzInfrastructure",
            return_value=infra,
        ),
        patch(
            "crsbench.utils.run_helper.ensure_oss_fuzz_root",
            return_value=str(tmp_path / "oss-fuzz"),
        ),
    ):
        rc = handle_image_prepare(args)

    assert rc == 0
    infra.create_variant_project.assert_called_once_with(
        benchmark_path=benchmarks_dir / benchmark_name,
        variant_name=benchmark_name,
    )
    infra.ensure_inc_image.assert_called_once_with(
        project_name=benchmark_name,
        sanitizer="address",
        benchmark_path=benchmarks_dir / benchmark_name,
        registry=args.registry,
        policy=args.policy,
        max_pull_bytes=args.max_pull_bytes,
        pull_timeout=args.pull_timeout,
        local_prefix=args.local_prefix,
    )


def test_image_build_creates_project_alias_before_local_build(tmp_path: Path) -> None:
    benchmarks_dir = tmp_path / "benchmarks"
    benchmarks_dir.mkdir()
    benchmark_name = "afc-curl-delta-01"
    _make_inc_benchmark(benchmarks_dir, benchmark_name)
    args = _build_args(benchmarks_dir)

    infra = Mock()
    infra.build_inc_build_image.return_value = True

    with (
        patch(
            "crsbench.builder.infrastructure.OSSFuzzInfrastructure",
            return_value=infra,
        ),
        patch(
            "crsbench.utils.run_helper.ensure_oss_fuzz_root",
            return_value=str(tmp_path / "oss-fuzz"),
        ),
    ):
        rc = handle_image_build(args)

    assert rc == 0
    infra.create_variant_project.assert_called_once_with(
        benchmark_path=benchmarks_dir / benchmark_name,
        variant_name=benchmark_name,
    )
    infra.build_inc_build_image.assert_called_once_with(
        project_name=benchmark_name,
        sanitizer="address",
        benchmark_path=benchmarks_dir / benchmark_name,
        local_prefix=args.local_prefix,
    )
