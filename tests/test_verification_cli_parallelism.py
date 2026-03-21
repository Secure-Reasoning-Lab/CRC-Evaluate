"""Tests for top-level verify/patch-verify parallelism flag resolution."""

import argparse
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from crsbench.builder.types import VariantType
from crsbench.evaluation.verification.models import (
    PatchVerificationResult,
    PatchVerificationStatus,
)


def _make_verification_fixture(tmp_path: Path) -> dict[str, Path]:
    benchmark_dir = tmp_path / "benchmark"
    (benchmark_dir / ".aixcc").mkdir(parents=True)
    (benchmark_dir / ".aixcc" / "meta.yaml").write_text("version: 1\n")

    pov_dir = tmp_path / "povs"
    pov_dir.mkdir()
    pov_path = pov_dir / "pov_0.blob"
    pov_path.write_bytes(b"\x00\x01\x02")

    patch_path = tmp_path / "patch.diff"
    patch_path.write_text("--- a/file.c\n+++ b/file.c\n")

    oss_fuzz_dir = tmp_path / "oss-fuzz"
    (oss_fuzz_dir / "infra").mkdir(parents=True)
    (oss_fuzz_dir / "infra" / "helper.py").write_text("# mock\n")

    return {
        "benchmark": benchmark_dir,
        "pov_dir": pov_dir,
        "pov": pov_path,
        "patch": patch_path,
        "oss_fuzz": oss_fuzz_dir,
    }


@pytest.fixture
def verify_cli_parser() -> argparse.ArgumentParser:
    from crsbench.evaluation.verification.cli.pov_verify_command import (
        add_verify_subparser,
    )

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_verify_subparser(subparsers)
    return parser


@pytest.fixture
def patch_verify_cli_parser() -> argparse.ArgumentParser:
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        add_patch_verify_subparser,
    )

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_patch_verify_subparser(subparsers)
    return parser


def test_verify_parser_accepts_jobs_and_cores_per_job(
    verify_cli_parser: argparse.ArgumentParser, tmp_path: Path
) -> None:
    fixture = _make_verification_fixture(tmp_path)

    args = verify_cli_parser.parse_args(
        [
            "verify",
            str(fixture["benchmark"]),
            "--pov-dir",
            str(fixture["pov_dir"]),
            "--jobs",
            "3",
            "--cores-per-job",
            "2",
        ]
    )

    assert args.jobs == 3
    assert args.cores_per_job == 2


def test_patch_verify_parser_accepts_jobs_and_cores_per_job(
    patch_verify_cli_parser: argparse.ArgumentParser, tmp_path: Path
) -> None:
    fixture = _make_verification_fixture(tmp_path)

    args = patch_verify_cli_parser.parse_args(
        [
            "patch-verify",
            str(fixture["benchmark"]),
            "--patch",
            str(fixture["patch"]),
            "--pov",
            str(fixture["pov"]),
            "--jobs",
            "4",
            "--cores-per-job",
            "2",
        ]
    )

    assert args.jobs == 4
    assert args.cores_per_job == 2


def test_run_verify_maps_jobs_and_cores_per_job_to_engine(tmp_path: Path) -> None:
    from crsbench.evaluation.verification.cli.pov_verify_command import run_verify

    fixture = _make_verification_fixture(tmp_path)
    fake_engine = MagicMock()
    fake_engine.verify_benchmark.return_value = SimpleNamespace(results=[])

    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        pov=None,
        pov_dir=fixture["pov_dir"],
        harness=None,
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        force_rebuild=False,
        dedup_strategy="patch-based",
        top_n=5,
        timeout=120,
        output=None,
        format="json",
        verbose=False,
        jobs=3,
        cores_per_job=2,
        build_workers=None,
        verify_workers=None,
    )

    with (
        patch(
            "crsbench.evaluation.verification.cli.pov_verify_command.get_dedup_strategy",
            return_value=object(),
        ),
        patch(
            "crsbench.evaluation.verification.cli.pov_verify_command.VerificationEngine",
            return_value=fake_engine,
        ) as mock_engine,
    ):
        result = run_verify(args)

    assert result == 0
    assert mock_engine.call_args.kwargs["jobs"] == 3
    assert mock_engine.call_args.kwargs["cores_per_job"] == 2


def test_run_verify_rejects_conflicting_parallelism_flags(tmp_path: Path) -> None:
    from crsbench.evaluation.verification.cli.pov_verify_command import run_verify

    fixture = _make_verification_fixture(tmp_path)
    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        pov=None,
        pov_dir=fixture["pov_dir"],
        harness=None,
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        force_rebuild=False,
        dedup_strategy="patch-based",
        top_n=5,
        timeout=120,
        output=None,
        format="json",
        verbose=False,
        jobs=3,
        cores_per_job=2,
        build_workers=4,
        verify_workers=None,
    )

    result = run_verify(args)

    assert result == 1


def test_run_patch_verify_maps_jobs_and_cores_per_job_to_engine(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        run_patch_verify,
    )

    fixture = _make_verification_fixture(tmp_path)
    discovery_engine = MagicMock()
    build_engine = MagicMock()
    verify_engine = MagicMock()
    build_engine.verify_patch.return_value = PatchVerificationResult(
        status=PatchVerificationStatus.VALID,
        patch_id="patch",
        pov_id="pov_0",
        benchmark=fixture["benchmark"].name,
        patch_path=fixture["patch"],
    )
    verify_engine.verify_patch.return_value = PatchVerificationResult(
        status=PatchVerificationStatus.VALID,
        patch_id="patch",
        pov_id="pov_0",
        benchmark=fixture["benchmark"].name,
        patch_path=fixture["patch"],
    )

    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        patch=fixture["patch"],
        patch_dir=None,
        pov=fixture["pov"],
        pov_dir=None,
        harness="fuzz_test",
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        test_mode="full",
        sanitizer="address",
        timeout=120,
        build_timeout=1200,
        test_timeout=1800,
        no_variants=False,
        force_rebuild=False,
        inc_build=False,
        output=None,
        format="text",
        verbose=False,
        jobs=4,
        cores_per_job=2,
        build_workers=None,
        verify_workers=None,
    )

    with (
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.PatchVerificationEngine",
            side_effect=[discovery_engine, build_engine, verify_engine],
        ) as mock_engine,
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command._resolve_harness_names",
            return_value=["fuzz_test"],
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.output_results"
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.print_summary"
        ),
    ):
        result = run_patch_verify(args)

    assert result == 0
    assert [call.kwargs["jobs"] for call in mock_engine.call_args_list] == [
        4,
        4,
        4,
    ]
    assert [call.kwargs["cores_per_job"] for call in mock_engine.call_args_list] == [
        2,
        2,
        2,
    ]


def test_run_patch_verify_rejects_conflicting_parallelism_flags(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        run_patch_verify,
    )

    fixture = _make_verification_fixture(tmp_path)
    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        patch=fixture["patch"],
        patch_dir=None,
        pov=fixture["pov"],
        pov_dir=None,
        harness="fuzz_test",
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        test_mode="full",
        sanitizer="address",
        timeout=120,
        build_timeout=1200,
        test_timeout=1800,
        no_variants=False,
        force_rebuild=False,
        inc_build=False,
        output=None,
        format="text",
        verbose=False,
        jobs=4,
        cores_per_job=2,
        build_workers=None,
        verify_workers=3,
    )

    result = run_patch_verify(args)

    assert result == 1


def test_run_patch_verify_directory_mode_uses_jobs_for_parallel_patch_tasks(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        run_patch_verify,
    )

    fixture = _make_verification_fixture(tmp_path)
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    (patch_dir / "patch-1.diff").write_text("--- a\n+++ b\n")
    (patch_dir / "patch-2.diff").write_text("--- a\n+++ b\n")

    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        patch=None,
        patch_dir=patch_dir,
        pov=None,
        pov_dir=fixture["pov_dir"],
        harness="fuzz_test",
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        test_mode="full",
        sanitizer="address",
        timeout=120,
        build_timeout=1200,
        test_timeout=1800,
        no_variants=False,
        force_rebuild=False,
        inc_build=False,
        output=None,
        format="json",
        verbose=False,
        jobs=2,
        cores_per_job=1,
        build_workers=None,
        verify_workers=None,
    )

    lock = threading.Lock()
    active = 0
    max_active = 0

    class FakePatchEngine:
        def __init__(self, *_args, **kwargs):
            self.build_only = kwargs["build_only"]

        def cleanup(self) -> None:
            return None

        def _infer_single_pov_id(self, _pov_dir: Path) -> str:
            return "cpv_0"

        def _discover_patches(self, _patch_dir: Path, target_pov_id: str | None = None):
            assert target_pov_id == "cpv_0"
            return fake_patches

        def _find_pov_for_patch(self, _pov_dir: Path, _pov_id: str) -> Path:
            return fixture["pov"]

        def verify_patch(self, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id=kwargs["patch"].patch_id,
                pov_id=kwargs["patch"].pov_id,
                benchmark=fixture["benchmark"].name,
                patch_path=kwargs["patch"].patch_path,
                harness=kwargs["harness"],
            )

    fake_patches = [
        SimpleNamespace(
            patch_id="patch-1",
            pov_id="cpv_0",
            patch_path=patch_dir / "patch-1.diff",
        ),
        SimpleNamespace(
            patch_id="patch-2",
            pov_id="cpv_0",
            patch_path=patch_dir / "patch-2.diff",
        ),
    ]

    with (
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.PatchVerificationEngine",
            side_effect=lambda *a, **k: FakePatchEngine(*a, **k),
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command._resolve_harness_names",
            return_value=["fuzz_test"],
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.output_results"
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.print_summary"
        ),
    ):
        result = run_patch_verify(args)

    assert result == 0
    assert max_active >= 2


def test_run_patch_verify_skips_duplicate_variant_tasks_across_harnesses(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        run_patch_verify,
    )

    fixture = _make_verification_fixture(tmp_path)
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    (patch_dir / "patch-1.diff").write_text("--- a\n+++ b\n")

    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        patch=None,
        patch_dir=patch_dir,
        pov=None,
        pov_dir=fixture["pov_dir"],
        harness=None,
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        test_mode="full",
        sanitizer="address",
        timeout=120,
        build_timeout=1200,
        test_timeout=1800,
        no_variants=False,
        force_rebuild=True,
        inc_build=False,
        output=None,
        format="json",
        verbose=False,
        jobs=2,
        cores_per_job=1,
        build_workers=None,
        verify_workers=None,
    )

    verify_calls: list[tuple[str, str, bool]] = []

    class FakePatchEngine:
        def __init__(self, *_args, **kwargs):
            self.build_only = kwargs["build_only"]

        def cleanup(self) -> None:
            return None

        def _infer_single_pov_id(self, _pov_dir: Path) -> str:
            return "cpv_0"

        def _discover_patches(
            self, _patch_dir: Path, target_pov_id: str | None = None
        ) -> list[SimpleNamespace]:
            assert target_pov_id == "cpv_0"
            return [
                SimpleNamespace(
                    patch_id="patch-1",
                    pov_id="cpv_0",
                    patch_path=patch_dir / "patch-1.diff",
                )
            ]

        def _find_pov_for_patch(self, _pov_dir: Path, _pov_id: str) -> Path:
            return fixture["pov"]

        def verify_patch(self, **kwargs) -> PatchVerificationResult:
            verify_calls.append(
                (
                    kwargs["patch"].patch_id,
                    kwargs["harness"],
                    self.build_only,
                )
            )
            return PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id=kwargs["patch"].patch_id,
                pov_id=kwargs["patch"].pov_id,
                benchmark=fixture["benchmark"].name,
                patch_path=kwargs["patch"].patch_path,
                harness=kwargs["harness"],
            )

    with (
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.PatchVerificationEngine",
            side_effect=lambda *a, **k: FakePatchEngine(*a, **k),
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command._resolve_harness_names",
            return_value=["harness-a", "harness-b"],
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.output_results"
        ),
        patch(
            "crsbench.evaluation.verification.cli.patch_verify_command.print_summary"
        ),
    ):
        result = run_patch_verify(args)

    assert result == 0
    assert verify_calls == [
        ("patch-1", "harness-a", True),
        ("patch-1", "harness-a", False),
    ]


def test_run_verify_single_pov_uses_cores_per_job_for_runtime_parallel_verification(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.verification.cli.pov_verify_command import run_verify

    fixture = _make_verification_fixture(tmp_path)
    args = argparse.Namespace(
        benchmark_path=fixture["benchmark"],
        pov=fixture["pov"],
        pov_dir=None,
        harness=None,
        oss_fuzz_path=fixture["oss_fuzz"],
        source="main_repo",
        force_rebuild=False,
        dedup_strategy="patch-based",
        top_n=5,
        timeout=120,
        output=None,
        format="json",
        verbose=False,
        jobs=1,
        cores_per_job=2,
        build_workers=None,
        verify_workers=None,
    )

    adapter = SimpleNamespace(
        benchmark_name=fixture["benchmark"].name,
        get_harness_names=lambda: ["harness-a", "harness-b"],
        get_mode=lambda: SimpleNamespace(value="delta"),
    )
    build_results = {
        "variant-a": SimpleNamespace(
            success=True,
            config=SimpleNamespace(
                variant_type=VariantType.DELTA_REF,
                cpv_num=None,
                sanitizer="address",
            ),
        ),
        "variant-b": SimpleNamespace(
            success=True,
            config=SimpleNamespace(
                variant_type=VariantType.ALL_PATCHED,
                cpv_num=None,
                sanitizer="address",
            ),
        ),
    }

    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_execute(_self, task):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return SimpleNamespace(
            pov_id=task.pov_id,
            harness=task.harness,
            variant_name=task.variant_name,
            variant_type=task.variant_type,
            cpv_num=task.cpv_num,
            crashed=task.variant_type == VariantType.DELTA_REF,
            crash_log="stdout",
            stderr="",
        )

    with (
        patch(
            "crsbench.evaluation.verification.cli.pov_verify_command.get_dedup_strategy",
            return_value=object(),
        ),
        patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine.load_adapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine.get_or_build_results",
            return_value=build_results,
        ),
        patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine._execute_reproduce",
            new=fake_execute,
        ),
        patch("crsbench.evaluation.verification.cli.pov_verify_command.output_results"),
        patch("crsbench.evaluation.verification.cli.pov_verify_command.print_summary"),
    ):
        result = run_verify(args)

    assert result == 0
    assert max_active >= 2
