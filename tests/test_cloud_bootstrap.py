from __future__ import annotations

from pathlib import Path

import pytest

from crsbench.validation.schemas import ExperimentConfig


def _base_kwargs() -> dict[str, object]:
    return {
        "experiment": "cloud-bootstrap-test",
        "trials": 1,
        "mode": "delta",
        "max_total_time": 20000,
        "inputs": {"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        "experiment_filestore": "/tmp/exp",
        "report_filestore": "/tmp/rep",
        "crs_compose": {"test-crs": {"num_cores": 1}},
        "cloud": {
            "gce": {
                "project": "test-project",
                "zone": "us-central1-a",
                "worker_count": 1,
                "machine_type": "e2-standard-4",
                "boot_disk_size_gb": 100,
                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                "owner_label": "team-crs",
            }
        },
    }


def _make_suite_config(
    suite_name: str,
    *,
    cloud_bootstrap: dict[str, object] | None = None,
    benchmarks_root: str | None = None,
    benchmark_suites_root: str | None = None,
) -> ExperimentConfig:
    data = _base_kwargs()
    data["benchmark_suite"] = suite_name
    if benchmarks_root is not None:
        data["benchmarks_root"] = benchmarks_root
    if benchmark_suites_root is not None:
        data["benchmark_suites_root"] = benchmark_suites_root
    if cloud_bootstrap is not None:
        cloud = dict(data["cloud"])  # type: ignore[arg-type]
        cloud["bootstrap"] = cloud_bootstrap
        data["cloud"] = cloud
    return ExperimentConfig(**data)


def _make_benchmarks_config(
    benchmarks: list[str],
    *,
    cloud_bootstrap: dict[str, object] | None = None,
    benchmarks_root: str | None = None,
) -> ExperimentConfig:
    data = _base_kwargs()
    data["benchmarks"] = benchmarks
    if benchmarks_root is not None:
        data["benchmarks_root"] = benchmarks_root
    if cloud_bootstrap is not None:
        cloud = dict(data["cloud"])  # type: ignore[arg-type]
        cloud["bootstrap"] = cloud_bootstrap
        data["cloud"] = cloud
    return ExperimentConfig(**data)


def test_should_download_benchmarks_auto_skips_sanity() -> None:
    from crsbench.cloud.bootstrap import (
        CloudVmBootstrapInputs,
        should_download_benchmarks,
    )

    config = _make_suite_config("sanity")

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert should_download_benchmarks(inputs) is False


def test_should_download_benchmarks_auto_downloads_non_sanity_suite() -> None:
    from crsbench.cloud.bootstrap import (
        CloudVmBootstrapInputs,
        should_download_benchmarks,
    )

    config = _make_suite_config("afc-final")

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert should_download_benchmarks(inputs) is True


def test_should_download_benchmarks_auto_downloads_explicit_benchmarks() -> None:
    from crsbench.cloud.bootstrap import (
        CloudVmBootstrapInputs,
        should_download_benchmarks,
    )

    config = _make_benchmarks_config(["afc-curl-delta-01"])

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert should_download_benchmarks(inputs) is True


def test_should_download_benchmarks_never_always_skips() -> None:
    from crsbench.cloud.bootstrap import (
        CloudVmBootstrapInputs,
        should_download_benchmarks,
    )

    config = _make_suite_config(
        "afc-final",
        cloud_bootstrap={"download_benchmarks": "never"},
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert should_download_benchmarks(inputs) is False


def test_should_download_benchmarks_always_always_downloads() -> None:
    from crsbench.cloud.bootstrap import (
        CloudVmBootstrapInputs,
        should_download_benchmarks,
    )

    config = _make_suite_config(
        "sanity",
        cloud_bootstrap={"download_benchmarks": "always"},
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert should_download_benchmarks(inputs) is True


def test_cloud_vm_bootstrap_inputs_preserves_suite_selector_and_defaults() -> None:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs

    config = _make_suite_config("afc-final")

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert inputs.prepare_mode == "full"
    assert inputs.download_benchmarks == "auto"
    assert inputs.selector.benchmark_suite == "afc-final"
    assert inputs.selector.benchmarks is None
    assert inputs.selector.benchmarks_root == config.benchmarks_root
    assert inputs.selector.benchmark_suites_root == config.benchmark_suites_root


def test_cloud_vm_bootstrap_inputs_preserves_explicit_benchmarks_and_custom_root() -> (
    None
):
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs

    config = _make_benchmarks_config(
        ["afc-curl-delta-01", "afc-curl-delta-02"],
        benchmarks_root="/mnt/benchmarks",
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert inputs.selector.benchmark_suite is None
    assert inputs.selector.benchmarks == [
        "afc-curl-delta-01",
        "afc-curl-delta-02",
    ]
    assert inputs.selector.benchmarks_root == Path("/mnt/benchmarks")


def test_cloud_vm_bootstrap_inputs_preserves_custom_suite_root() -> None:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs

    config = _make_suite_config(
        "afc-final",
        benchmark_suites_root="/srv/crsbench/benchmark-suites",
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert inputs.selector.benchmark_suites_root == Path(
        "/srv/crsbench/benchmark-suites"
    )


def test_prepare_command_args_for_full_mode() -> None:
    from crsbench.cloud.bootstrap import prepare_command_args

    assert prepare_command_args("full") == ["crsbench", "prepare"]


def test_prepare_command_args_for_skip_base_images_mode() -> None:
    from crsbench.cloud.bootstrap import prepare_command_args

    assert prepare_command_args("skip_base_images") == [
        "crsbench",
        "prepare",
        "--skip-base-images",
    ]


def test_run_prepare_invokes_runner_with_repo_cwd(tmp_path: Path) -> None:
    from crsbench.cloud.bootstrap import run_prepare

    calls: list[tuple[list[str], Path, bool]] = []

    def _runner(cmd: list[str], *, cwd: Path, check: bool) -> None:
        calls.append((cmd, cwd, check))

    run_prepare("skip_base_images", cwd=tmp_path, runner=_runner)

    assert calls == [(["crsbench", "prepare", "--skip-base-images"], tmp_path, True)]


def test_run_benchmark_download_for_suite_uses_download_suite(tmp_path: Path) -> None:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs, run_benchmark_download

    config = _make_suite_config("afc-final")
    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    calls: list[tuple[str, Path, Path, bool]] = []

    def _download_suite(
        suite_name: str,
        output_dir: Path,
        suites_root: Path,
        *,
        no_ground_truth: bool,
    ) -> list[Path]:
        calls.append((suite_name, output_dir, suites_root, no_ground_truth))
        return [output_dir]

    run_benchmark_download(
        inputs,
        download_suite_fn=_download_suite,
    )

    assert calls == [
        (
            "afc-final",
            config.benchmarks_root,
            config.benchmark_suites_root,
            False,
        )
    ]


def test_run_benchmark_download_for_explicit_benchmarks_groups_by_dataset() -> None:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs, run_benchmark_download

    config = _make_benchmarks_config(
        [
            "afc-curl-delta-01",
            "afc-curl-delta-02",
        ]
    )
    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    calls: list[tuple[str, Path, list[str], bool]] = []

    def _download_dataset(
        dataset: str,
        output_dir: Path,
        *,
        benchmarks: list[str] | None,
        no_ground_truth: bool,
    ) -> Path:
        calls.append((dataset, output_dir, benchmarks or [], no_ground_truth))
        return output_dir

    run_benchmark_download(
        inputs,
        download_dataset_fn=_download_dataset,
    )

    assert calls == [
        (
            "crsbench",
            config.benchmarks_root,
            ["afc-curl-delta-01", "afc-curl-delta-02"],
            False,
        )
    ]


def test_run_benchmark_download_rejects_missing_selector() -> None:
    from crsbench.cloud.bootstrap import (
        CloudBenchmarkSelector,
        CloudVmBootstrapInputs,
        run_benchmark_download,
    )

    inputs = CloudVmBootstrapInputs(
        prepare_mode="full",
        download_benchmarks="always",
        selector=CloudBenchmarkSelector(
            benchmark_suite=None,
            benchmarks=None,
            benchmarks_root=Path("benchmarks"),
            benchmark_suites_root=Path("benchmark-suites"),
        ),
    )

    with pytest.raises(ValueError, match="benchmark selector"):
        run_benchmark_download(inputs)
