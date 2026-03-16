"""Tests for Atlantis-backed UniAFL coverage build/runtime helpers."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_write_coverage_compose_yaml_uses_local_atlantis_checkout(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import write_coverage_compose_yaml

    compose_path = tmp_path / "crs-compose.yaml"
    atlantis_root = tmp_path / "atlantis"
    atlantis_root.mkdir()

    write_coverage_compose_yaml(
        compose_path=compose_path,
        uniafl_root=atlantis_root,
        cpuset="4-5",
        memory="8192MB",
    )

    text = compose_path.read_text()
    assert "atlantis-multilang-given_fuzzer:" in text
    assert "local_path:" in text
    assert str(atlantis_root) in text
    assert "cpuset: 4-5" in text
    assert "memory: 8192MB" in text


def test_write_coverage_compose_yaml_uses_shared_default_memory_when_unspecified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import write_coverage_compose_yaml

    compose_path = tmp_path / "crs-compose.yaml"
    atlantis_root = tmp_path / "atlantis"
    atlantis_root.mkdir()
    monkeypatch.setenv("CRSBENCH_OSS_CRS_DEFAULT_MEMORY", "32768MB")

    write_coverage_compose_yaml(
        compose_path=compose_path,
        uniafl_root=atlantis_root,
        cpuset="0",
    )

    text = compose_path.read_text()
    assert text.count("memory: 32768MB") == 2


def test_materialize_atlantis_build_output_links_runtime_layout(tmp_path: Path) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import (
        materialize_atlantis_build_output,
    )

    atlantis_out = tmp_path / "atlantis-build"
    (atlantis_out / "uniafl" / "build").mkdir(parents=True)
    (atlantis_out / "uniafl" / "src").mkdir(parents=True)
    (atlantis_out / "coverage" / "build").mkdir(parents=True)
    (atlantis_out / "uniafl" / "build" / "fuzz_target").write_text("#!/bin/sh\n")
    (atlantis_out / "coverage" / "build" / "fuzz_target").write_text("#!/bin/sh\n")

    normalized_out = tmp_path / "normalized-out"
    source_repo_dir = materialize_atlantis_build_output(
        atlantis_build_output_dir=atlantis_out,
        normalized_build_output_dir=normalized_out,
    )

    assert source_repo_dir == normalized_out / ".crsbench-repo"
    assert (normalized_out / "fuzz_target").is_file()
    assert not (normalized_out / "fuzz_target").is_symlink()
    assert (normalized_out / ".crsbench-repo").is_symlink()
    assert (normalized_out / "coverage-out").is_dir()
    assert not (normalized_out / "coverage-out").is_symlink()
    assert (normalized_out / "coverage-out" / "fuzz_target").exists()


def test_materialize_atlantis_build_output_allows_skipped_coverage_build(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import (
        materialize_atlantis_build_output,
    )

    atlantis_out = tmp_path / "atlantis-build"
    (atlantis_out / "uniafl" / "build").mkdir(parents=True)
    (atlantis_out / "uniafl" / "src").mkdir(parents=True)
    (atlantis_out / "coverage").mkdir(parents=True)
    (atlantis_out / "coverage" / ".build.skip").write_text("")
    (atlantis_out / "uniafl" / "build" / "FuzzTarget").write_text("#!/bin/sh\n")

    normalized_out = tmp_path / "normalized-out"
    materialize_atlantis_build_output(
        atlantis_build_output_dir=atlantis_out,
        normalized_build_output_dir=normalized_out,
    )

    assert (normalized_out / "FuzzTarget").is_file()
    assert not (normalized_out / "FuzzTarget").is_symlink()
    assert not (normalized_out / "coverage-out").exists()


def test_build_atlantis_coverage_artifacts_skips_prepare_when_images_exist(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import (
        build_atlantis_coverage_artifacts,
    )

    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    normalized_build_output_dir = tmp_path / "normalized-out"
    control_root = tmp_path / "control"
    resolved_out = tmp_path / "atlantis-build"
    source_repo = tmp_path / "source-repo"
    atlantis_root = tmp_path / "atlantis"
    atlantis_root = tmp_path / "atlantis"

    with (
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.write_coverage_compose_yaml"
        ) as mock_write_compose,
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.stage_benchmark_for_coverage"
        ) as mock_stage,
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.prepare_images_reusable",
            return_value=True,
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.prepare_uniafl_backend"
        ) as mock_prepare,
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.run_oss_crs_build_target",
            return_value=("build ok", "", 0),
        ) as mock_build_target,
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime._resolve_atlantis_build_output",
            return_value=(resolved_out, "build-123"),
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.materialize_atlantis_build_output",
            return_value=source_repo,
        ),
    ):
        build = build_atlantis_coverage_artifacts(
            benchmark_path=benchmark_path,
            normalized_build_output_dir=normalized_build_output_dir,
            control_root=control_root,
        )

    mock_write_compose.assert_called_once()
    mock_stage.assert_called_once_with(
        benchmark_path,
        control_root / "staged" / benchmark_path.name,
    )
    mock_prepare.assert_not_called()
    mock_build_target.assert_called_once()
    assert build.build_id == "build-123"
    assert build.atlantis_build_output_dir == resolved_out
    assert build.source_repo_dir == source_repo


def test_build_atlantis_coverage_artifacts_refreshes_prepare_state_after_prepare(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import (
        build_atlantis_coverage_artifacts,
    )

    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    normalized_build_output_dir = tmp_path / "normalized-out"
    control_root = tmp_path / "control"
    resolved_out = tmp_path / "atlantis-build"
    source_repo = tmp_path / "source-repo"
    atlantis_root = tmp_path / "atlantis"

    with (
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.write_coverage_compose_yaml"
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.stage_benchmark_for_coverage"
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.prepare_images_reusable",
            return_value=False,
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.prepare_uniafl_backend",
            return_value=0,
        ) as mock_prepare,
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.run_oss_crs_build_target",
            return_value=("build ok", "", 0),
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime._resolve_atlantis_build_output",
            return_value=(resolved_out, "build-123"),
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.materialize_atlantis_build_output",
            return_value=source_repo,
        ),
    ):
        build_atlantis_coverage_artifacts(
            benchmark_path=benchmark_path,
            normalized_build_output_dir=normalized_build_output_dir,
            control_root=control_root,
            uniafl_root=atlantis_root,
        )

    mock_prepare.assert_called_once_with(atlantis_root.resolve())


def test_load_oss_crs_runtime_classes_matches_repo_layout() -> None:
    from crsbench.evaluation.coverage.uniafl_runtime import (
        _load_oss_crs_runtime_classes,
    )

    compose_cls, target_cls = _load_oss_crs_runtime_classes()

    assert compose_cls.__name__ == "CRSCompose"
    assert target_cls.__name__ == "Target"


def test_load_oss_crs_runtime_classes_falls_back_to_repo_checkout(
    monkeypatch,
) -> None:
    from crsbench.evaluation.coverage import uniafl_runtime

    uniafl_runtime._load_oss_crs_runtime_classes.cache_clear()
    repo_oss_crs = Path(uniafl_runtime.__file__).resolve().parents[3] / "oss-crs"
    fake_compose = SimpleNamespace(CRSCompose=type("CRSCompose", (), {}))
    fake_target = SimpleNamespace(Target=type("Target", (), {}))
    seen_paths: list[list[str]] = []

    def fake_import_module(name: str):
        seen_paths.append(list(sys.path))
        if name == "oss_crs.src.crs_compose" and len(seen_paths) == 1:
            raise ModuleNotFoundError("missing oss_crs")
        if name == "oss_crs.src.crs_compose":
            return fake_compose
        if name == "oss_crs.src.target":
            return fake_target
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(uniafl_runtime.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(
        sys,
        "path",
        [entry for entry in sys.path if entry != str(repo_oss_crs)],
    )

    compose_cls, target_cls = uniafl_runtime._load_oss_crs_runtime_classes()

    assert compose_cls.__name__ == "CRSCompose"
    assert target_cls.__name__ == "Target"
    assert str(repo_oss_crs) not in seen_paths[0]
    assert str(repo_oss_crs) in sys.path

    uniafl_runtime._load_oss_crs_runtime_classes.cache_clear()
