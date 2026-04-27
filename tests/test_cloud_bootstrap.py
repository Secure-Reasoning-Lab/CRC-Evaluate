"""Unit tests for provider-neutral cloud VM bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

import crsbench.cloud.bootstrap as bootstrap_module
import pytest
from crsbench.cloud.bootstrap import (
    CloudBenchmarkSelector,
    CloudVmBootstrapInputs,
    bootstrap_inputs_from_payload,
    build_download_delay_schedule,
    prepare_command_args,
    run_benchmark_download,
    run_benchmark_download_with_delay,
    run_cloud_vm_bootstrap,
    run_prepare,
    should_download_benchmarks,
)
from crsbench.validation.schemas import ExperimentConfig


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


def test_bootstrap_inputs_from_payload_restores_defaults_and_explicit_fields():
    inputs = bootstrap_inputs_from_payload(
        {
            "prepare_mode": "skip_base_images",
            "download_benchmarks": "always",
            "benchmark_suite": "afc-final",
            "gitcache": True,
            "benchmarks_root": "benchmarks",
            "benchmark_suites_root": "benchmark-suites-custom",
            "oss_fuzz_path": "third_party/oss-fuzz-custom",
        }
    )

    assert inputs.prepare_mode == "skip_base_images"
    assert inputs.download_benchmarks == "always"
    assert inputs.benchmark_suite == "afc-final"
    assert inputs.gitcache is True
    assert inputs.benchmarks is None
    assert inputs.benchmarks_root == Path("benchmarks")
    assert inputs.benchmark_suites_root == Path("benchmark-suites-custom")
    assert inputs.oss_fuzz_path == Path("third_party/oss-fuzz-custom")


def test_from_experiment_config_restores_repo_relative_managed_oss_fuzz_paths() -> None:
    repo_root = bootstrap_module.CRSBENCH_REPO_ROOT
    config = ExperimentConfig(
        experiment="cloud-bootstrap-managed-oss-fuzz",
        trials=1,
        mode="full",
        max_total_time=20000,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep",
        benchmarks=["go-yaml"],
        benchmarks_root=str(repo_root / "third_party" / "oss-fuzz" / "projects"),
        oss_fuzz_path=str(repo_root / "third_party" / "oss-fuzz"),
        crs_compose={"test-crs": {"num_cores": 1}},
        cloud={
            "bootstrap": {"download_benchmarks": "auto"},
            "providers": {
                "gce": {
                    "project": "test-project",
                    "profile_defaults": {
                        "machine_type": "e2-standard-4",
                        "boot_disk_size_gb": 100,
                        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                        "service_account_email": "crsbench@test-project.iam.gserviceaccount.com",
                        "owner_label": "team-crs",
                    },
                    "instance_profiles": {
                        "gce-orchestrator-default": {
                            "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
                        },
                        "gce-worker-default": {
                            "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                        },
                    },
                }
            },
            "orchestrator": {
                "zone": "us-central1-a",
                "instance_profile": "gce-orchestrator-default",
            },
            "workers": {
                "defaults": {
                    "instance_profile": "gce-worker-default",
                    "count": 1,
                },
                "placements": [
                    {
                        "zone": "us-central1-a",
                    }
                ],
            },
        },
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert inputs.benchmarks_root == Path("third_party/oss-fuzz/projects")
    assert inputs.oss_fuzz_path == Path("third_party/oss-fuzz")


def test_build_download_delay_schedule_uses_conservative_priority_waves() -> None:
    assert build_download_delay_schedule(
        orchestrator_name="crsbench-exp-orch",
        worker_names=[
            "crsbench-exp-work-001",
            "crsbench-exp-work-002",
            "crsbench-exp-work-003",
            "crsbench-exp-work-004",
            "crsbench-exp-work-005",
        ],
        evaluator_names=[
            "crsbench-exp-eval-001",
            "crsbench-exp-eval-002",
            "crsbench-exp-eval-003",
        ],
    ) == {
        "crsbench-exp-orch": 0,
        "crsbench-exp-work-001": 10,
        "crsbench-exp-eval-001": 20,
        "crsbench-exp-eval-002": 300,
        "crsbench-exp-eval-003": 310,
        "crsbench-exp-work-002": 320,
        "crsbench-exp-work-003": 600,
        "crsbench-exp-work-004": 610,
        "crsbench-exp-work-005": 620,
    }


def test_run_benchmark_download_with_delay_sleeps_before_download(monkeypatch) -> None:
    sleep_calls: list[int] = []
    download_calls: list[str] = []

    def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)

    def fake_download(selector: CloudBenchmarkSelector) -> list[Path]:
        download_calls.append(selector.benchmark_suite or "benchmarks")
        return [Path("/tmp/benchmarks")]

    monkeypatch.setattr(bootstrap_module.time, "sleep", fake_sleep)

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(benchmark_suite="afc-final")
    )

    result = run_benchmark_download_with_delay(
        selector,
        download_delay_sec=310,
        download_fn=fake_download,
    )

    assert sleep_calls == [310]
    assert download_calls == ["afc-final"]
    assert result == [Path("/tmp/benchmarks")]


def test_run_cloud_vm_bootstrap_reads_download_delay_from_env(
    monkeypatch, tmp_path: Path
) -> None:
    prepare_calls: list[tuple[str, Path]] = []
    download_calls: list[tuple[str | None, int, bool]] = []

    def fake_run_prepare(
        prepare_mode: str,
        *,
        cwd: Path | None = None,
        runner=None,
    ) -> None:
        del runner
        assert cwd is not None
        prepare_calls.append((prepare_mode, cwd))

    def fake_run_benchmark_download_with_delay(
        selector: CloudBenchmarkSelector,
        *,
        download_delay_sec: int,
        download_fn,
    ) -> list[Path]:
        download_calls.append(
            (
                selector.benchmark_suite,
                download_delay_sec,
                download_fn is not None,
            )
        )
        return [Path("/tmp/benchmarks")]

    monkeypatch.setenv("CRSBENCH_DOWNLOAD_DELAY_SEC", "20")
    monkeypatch.setattr(bootstrap_module, "run_prepare", fake_run_prepare)
    monkeypatch.setattr(
        bootstrap_module,
        "run_benchmark_download_with_delay",
        fake_run_benchmark_download_with_delay,
    )

    result = run_cloud_vm_bootstrap(
        CloudVmBootstrapInputs(benchmark_suite="afc-final"),
        cwd=tmp_path,
    )

    assert prepare_calls == [("full", tmp_path)]
    assert download_calls == [("afc-final", 20, True)]
    assert result == [Path("/tmp/benchmarks")]


def test_run_cloud_vm_bootstrap_applies_download_delay_to_external_benchmarks(
    monkeypatch, tmp_path: Path
) -> None:
    prepare_calls: list[tuple[str, Path]] = []
    external_calls: list[tuple[tuple[str, ...], Path, Path]] = []
    delayed_download_calls: list[tuple[tuple[str, ...], int, bool]] = []

    def fake_run_prepare(
        prepare_mode: str,
        *,
        cwd: Path | None = None,
        runner=None,
    ) -> None:
        del runner
        assert cwd is not None
        prepare_calls.append((prepare_mode, cwd))

    def fake_prepare_external_benchmarks(
        selector: CloudBenchmarkSelector,
        *,
        cwd: Path | None,
        oss_fuzz_path: Path = Path("third_party/oss-fuzz"),
    ) -> list[Path] | None:
        assert cwd is not None
        assert oss_fuzz_path == Path("third_party/oss-fuzz")
        external_calls.append(
            (
                tuple(selector.benchmark_names()),
                selector.effective_benchmarks_root(),
                cwd,
            )
        )
        return [cwd / "third_party" / "oss-fuzz" / "projects" / "go-yaml"]

    def fake_run_benchmark_download_with_delay(
        selector: CloudBenchmarkSelector,
        *,
        download_delay_sec: int,
        download_fn,
    ) -> list[Path]:
        delayed_download_calls.append(
            (
                tuple(selector.benchmark_names()),
                download_delay_sec,
                download_fn is not None,
            )
        )
        assert download_fn is not None
        return download_fn(selector)

    monkeypatch.setenv("CRSBENCH_DOWNLOAD_DELAY_SEC", "20")
    monkeypatch.setattr(bootstrap_module, "run_prepare", fake_run_prepare)
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_external_benchmarks",
        fake_prepare_external_benchmarks,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "run_benchmark_download_with_delay",
        fake_run_benchmark_download_with_delay,
    )

    result = run_cloud_vm_bootstrap(
        CloudVmBootstrapInputs(
            benchmarks=["go-yaml"],
            benchmarks_root=Path("third_party/oss-fuzz/projects"),
        ),
        cwd=tmp_path,
    )

    assert prepare_calls == [("full", tmp_path)]
    assert external_calls == [
        (("go-yaml",), Path("third_party/oss-fuzz/projects"), tmp_path)
    ]
    assert delayed_download_calls == [(("go-yaml",), 20, True)]
    assert result == [tmp_path / "third_party" / "oss-fuzz" / "projects" / "go-yaml"]


def test_run_cloud_vm_bootstrap_skips_external_benchmark_prep_when_policy_is_never(
    monkeypatch, tmp_path: Path
) -> None:
    prepare_calls: list[tuple[str, Path]] = []
    external_calls: list[tuple[tuple[str, ...], Path, Path]] = []
    delayed_download_calls: list[tuple[tuple[str, ...], int, bool]] = []

    def fake_run_prepare(
        prepare_mode: str,
        *,
        cwd: Path | None = None,
        runner=None,
    ) -> None:
        del runner
        assert cwd is not None
        prepare_calls.append((prepare_mode, cwd))

    def fake_prepare_external_benchmarks(
        selector: CloudBenchmarkSelector,
        *,
        cwd: Path | None,
        oss_fuzz_path: Path = Path("third_party/oss-fuzz"),
    ) -> list[Path] | None:
        assert cwd is not None
        assert oss_fuzz_path == Path("third_party/oss-fuzz")
        external_calls.append(
            (
                tuple(selector.benchmark_names()),
                selector.effective_benchmarks_root(),
                cwd,
            )
        )
        return [cwd / "third_party" / "oss-fuzz" / "projects" / "go-yaml"]

    def fake_run_benchmark_download_with_delay(
        selector: CloudBenchmarkSelector,
        *,
        download_delay_sec: int,
        download_fn,
    ) -> list[Path]:
        delayed_download_calls.append(
            (
                tuple(selector.benchmark_names()),
                download_delay_sec,
                download_fn is not None,
            )
        )
        return [Path("/tmp/unexpected-download")]

    monkeypatch.setattr(bootstrap_module, "run_prepare", fake_run_prepare)
    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_external_benchmarks",
        fake_prepare_external_benchmarks,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "run_benchmark_download_with_delay",
        fake_run_benchmark_download_with_delay,
    )

    result = run_cloud_vm_bootstrap(
        CloudVmBootstrapInputs(
            benchmarks=["go-yaml"],
            benchmarks_root=Path("third_party/oss-fuzz/projects"),
            download_benchmarks="never",
        ),
        cwd=tmp_path,
    )

    assert prepare_calls == [("full", tmp_path)]
    assert external_calls == []
    assert delayed_download_calls == []
    assert result == []


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


def test_run_benchmark_download_materializes_managed_oss_fuzz_projects_and_inits_meta(
    monkeypatch, tmp_path: Path
) -> None:
    checkout_root = tmp_path
    managed_projects_root = checkout_root / "third_party" / "oss-fuzz" / "projects"
    managed_projects_root.mkdir(parents=True)

    materialized: list[tuple[str, Path, Path]] = []
    meta_calls: list[tuple[Path, Path]] = []
    dataset_calls: list[tuple[str, Path, list[str] | None, bool]] = []

    def fake_materialize(
        benchmark_name: str,
        *,
        oss_fuzz_root: Path,
    ) -> Path:
        materialized.append((benchmark_name, tmp_path, oss_fuzz_root))
        project_dir = oss_fuzz_root / "projects" / benchmark_name
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "project.yaml").write_text("language: c\n")
        (project_dir / "Dockerfile").write_text("FROM scratch\n")
        (project_dir / "build.sh").write_text("#!/bin/sh\n")
        return project_dir

    def fake_auto_generate_meta_yaml(
        benchmark_path: Path,
        oss_fuzz_path: Path,
        sanitizer: str = "address",
        *,
        cpuset_cpus: str | None = None,
    ) -> Path:
        del sanitizer, cpuset_cpus
        meta_calls.append((benchmark_path, oss_fuzz_path))
        meta_path = benchmark_path / ".aixcc" / "meta.yaml"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("full_mode:\n  base_commit: " + ("0" * 40) + "\n")
        return meta_path

    def fake_download_dataset(
        dataset: str,
        output_dir: Path,
        *,
        benchmarks: list[str] | None = None,
        no_ground_truth: bool = False,
    ) -> Path:
        dataset_calls.append((dataset, output_dir, benchmarks, no_ground_truth))
        return output_dir

    monkeypatch.setattr(
        bootstrap_module,
        "_materialize_managed_oss_fuzz_project",
        fake_materialize,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "auto_generate_meta_yaml",
        fake_auto_generate_meta_yaml,
    )
    monkeypatch.setattr(bootstrap_module, "download_dataset", fake_download_dataset)

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmarks=["go-yaml"],
            benchmarks_root=Path("third_party/oss-fuzz/projects"),
        )
    )

    result = run_benchmark_download(selector, cwd=checkout_root)

    project_dir = managed_projects_root / "go-yaml"
    assert result == [project_dir]
    assert materialized == [
        ("go-yaml", checkout_root, checkout_root / "third_party" / "oss-fuzz")
    ]
    assert meta_calls == [(project_dir, checkout_root / "third_party" / "oss-fuzz")]
    assert dataset_calls == []
    assert (project_dir / ".aixcc" / "meta.yaml").exists()


def test_run_benchmark_download_uses_existing_external_benchmark_under_unmanaged_root(
    monkeypatch, tmp_path: Path
) -> None:
    external_root = tmp_path / "external-benchmarks"
    custom_oss_fuzz_root = tmp_path / "custom-oss-fuzz"
    benchmark_dir = external_root / "go-yaml"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: go\n")
    (benchmark_dir / "Dockerfile").write_text("FROM scratch\n")
    (benchmark_dir / "build.sh").write_text("#!/bin/sh\n")
    dataset_calls: list[tuple[str, Path, list[str] | None, bool]] = []
    meta_calls: list[tuple[Path, Path]] = []

    def fake_download_dataset(
        dataset: str,
        output_dir: Path,
        *,
        benchmarks: list[str] | None = None,
        no_ground_truth: bool = False,
    ) -> Path:
        dataset_calls.append((dataset, output_dir, benchmarks, no_ground_truth))
        return output_dir

    def fake_auto_generate_meta_yaml(
        benchmark_path: Path,
        oss_fuzz_path: Path,
        sanitizer: str = "address",
        *,
        cpuset_cpus: str | None = None,
    ) -> Path:
        del sanitizer, cpuset_cpus
        meta_calls.append((benchmark_path, oss_fuzz_path))
        meta_path = benchmark_path / ".aixcc" / "meta.yaml"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("full_mode:\n  base_commit: " + ("0" * 40) + "\n")
        return meta_path

    monkeypatch.setattr(bootstrap_module, "download_dataset", fake_download_dataset)
    monkeypatch.setattr(
        bootstrap_module,
        "auto_generate_meta_yaml",
        fake_auto_generate_meta_yaml,
    )

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmarks=["go-yaml"],
            benchmarks_root=external_root,
        )
    )

    result = run_benchmark_download(
        selector,
        cwd=tmp_path,
        oss_fuzz_path=custom_oss_fuzz_root,
    )

    assert result == [benchmark_dir]
    assert dataset_calls == []
    assert meta_calls == [(benchmark_dir, custom_oss_fuzz_root)]
    assert (benchmark_dir / ".aixcc" / "meta.yaml").exists()


def test_run_benchmark_download_fails_for_missing_external_benchmark_under_unmanaged_root(
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external-benchmarks"
    external_root.mkdir(parents=True)

    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmarks=["go-yaml"],
            benchmarks_root=external_root,
        )
    )

    with pytest.raises(
        ValueError,
        match="External benchmark directories are missing under unmanaged benchmarks_root",
    ):
        run_benchmark_download(selector, cwd=tmp_path)


def test_run_benchmark_download_fails_for_mixed_dataset_and_external_benchmarks(
    tmp_path: Path,
) -> None:
    selector = CloudBenchmarkSelector.from_inputs(
        CloudVmBootstrapInputs(
            benchmarks=["afc-demo-01", "go-yaml"],
            benchmarks_root=Path("third_party/oss-fuzz/projects"),
        )
    )

    with pytest.raises(
        ValueError,
        match="cannot mix CRSBench dataset benchmarks with external benchmarks",
    ):
        run_benchmark_download(selector, cwd=tmp_path)


def test_from_experiment_config_restores_repo_default_roots() -> None:
    config = ExperimentConfig(
        experiment="cloud-bootstrap-test",
        trials=1,
        mode="delta",
        max_total_time=20000,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep",
        benchmark_suite="sanity",
        crs_compose={"test-crs": {"num_cores": 1}},
        cloud={
            "bootstrap": {
                "gitcache": True,
            },
            "providers": {
                "gce": {
                    "project": "test-project",
                    "profile_defaults": {
                        "machine_type": "e2-standard-4",
                        "boot_disk_size_gb": 100,
                        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                        "service_account_email": "crsbench@test-project.iam.gserviceaccount.com",
                        "owner_label": "team-crs",
                    },
                    "instance_profiles": {
                        "gce-orchestrator-default": {
                            "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
                        },
                        "gce-worker-default": {
                            "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                        },
                    },
                }
            },
            "orchestrator": {
                "zone": "us-central1-a",
                "instance_profile": "gce-orchestrator-default",
            },
            "workers": {
                "defaults": {
                    "instance_profile": "gce-worker-default",
                    "count": 1,
                },
                "placements": [
                    {
                        "zone": "us-central1-a",
                    }
                ],
            },
        },
    )

    inputs = CloudVmBootstrapInputs.from_experiment_config(config)

    assert inputs.gitcache is True
    assert inputs.benchmarks_root == Path("benchmarks")
    assert inputs.benchmark_suites_root == Path("benchmark-suites")
