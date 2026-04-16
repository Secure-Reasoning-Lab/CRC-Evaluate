"""Tests for top-level `crsbench prepare` command."""

from __future__ import annotations

import argparse
import subprocess

from crsbench.prepare.cli import add_prepare_subparser, run_prepare


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_prepare_subparser(subparsers)
    return parser


def test_prepare_parses_defaults() -> None:
    parser = _parser()
    args = parser.parse_args(["prepare"])
    assert args.command == "prepare"
    assert args.coverage is False
    assert args.skip_base_images is False
    assert args.build_base_images is False


def test_prepare_parses_supported_flags() -> None:
    parser = _parser()
    args = parser.parse_args(
        ["prepare", "--coverage", "--skip-base-images", "--build-base-images"]
    )
    assert args.command == "prepare"
    assert args.coverage is True
    assert args.skip_base_images is True
    assert args.build_base_images is True


def test_run_prepare_coverage_invokes_uniafl_backend(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=True,
        skip_base_images=False,
        build_base_images=False,
    )

    called = {"prepare": 0, "ensure": 0, "run": 0}

    monkeypatch.setattr(
        "crsbench.prepare.cli.prepare_uniafl_backend",
        lambda: called.__setitem__("prepare", called["prepare"] + 1) or 0,
    )
    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: called.__setitem__("ensure", called["ensure"] + 1) or "/tmp/oss-fuzz",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: called.__setitem__("run", called["run"] + 1),
    )

    assert run_prepare(args) == 0
    assert called == {"prepare": 1, "ensure": 0, "run": 0}


def test_run_prepare_coverage_rejects_conflicting_flags(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=True,
        skip_base_images=True,
        build_base_images=False,
    )

    called = {"prepare": 0}
    monkeypatch.setattr(
        "crsbench.prepare.cli.prepare_uniafl_backend",
        lambda: called.__setitem__("prepare", called["prepare"] + 1) or 0,
    )

    assert run_prepare(args) == 2
    assert called["prepare"] == 0


def test_run_prepare_coverage_backend_failure_returns_1(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=True,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.prepare_uniafl_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("prepare failed")),
    )

    assert run_prepare(args) == 1


def test_run_prepare_skip_base_images(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=True,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    called = {"run": 0}

    def _unexpected_run(*_args, **_kwargs):
        called["run"] += 1
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(subprocess, "run", _unexpected_run)

    assert run_prepare(args) == 0
    assert called["run"] == 0


def test_run_prepare_conflicting_noncoverage_flags(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=True,
        build_base_images=True,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    assert run_prepare(args) == 2


def test_run_prepare_bootstrap_failure_returns_1(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: (_ for _ in ()).throw(RuntimeError("bootstrap failed")),
    )

    assert run_prepare(args) == 1


def test_run_prepare_pulls_base_images(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    def _ensure_all_rts_images():
        calls.append("rts")
        return {
            "jvm": "crsbench-rts-jvm:latest",
            "c": "crsbench-rts-c:latest",
        }

    monkeypatch.setattr(
        "crsbench.builder.rts.dockerfile_swap.ensure_all_rts_images",
        _ensure_all_rts_images,
    )
    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_all_rts_images",
        _ensure_all_rts_images,
        raising=False,
    )

    assert run_prepare(args) == 0
    assert calls[0] == ["python3", "infra/helper.py", "pull_images"]
    assert any(
        cmd[:2] == ["docker", "pull"]
        and "ghcr.io/aixcc-finals/base-builder:v1.3.0" in cmd
        for cmd in calls[1:]
        if isinstance(cmd, list)
    )
    assert calls[-1] == "rts"


def test_run_prepare_builds_base_images_when_requested(monkeypatch, tmp_path) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=True,
    )

    oss_fuzz_root = tmp_path / "oss-fuzz"
    build_script = oss_fuzz_root / "infra" / "base-images" / "all.sh"
    build_script.parent.mkdir(parents=True)
    build_script.write_text("#!/bin/bash\n")

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: str(oss_fuzz_root),
    )

    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    def _ensure_all_rts_images():
        calls.append("rts")
        return {
            "jvm": "crsbench-rts-jvm:latest",
            "c": "crsbench-rts-c:latest",
        }

    monkeypatch.setattr(
        "crsbench.builder.rts.dockerfile_swap.ensure_all_rts_images",
        _ensure_all_rts_images,
    )
    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_all_rts_images",
        _ensure_all_rts_images,
        raising=False,
    )

    assert run_prepare(args) == 0
    assert calls[-2] == "rts"
    assert calls[-1] == ["bash", str(build_script)]


def test_run_prepare_pull_failure_returns_nonzero(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    def _run(cmd, **_kwargs):
        if cmd[:3] == ["python3", "infra/helper.py", "pull_images"]:
            return subprocess.CompletedProcess(cmd, 9, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    assert run_prepare(args) == 9


def test_run_prepare_aixcc_pull_failure_returns_nonzero(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    def _run(cmd, **_kwargs):
        if cmd[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(cmd, 7, stdout="", stderr="broken")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    assert run_prepare(args) == 7


def test_run_prepare_skip_base_images_skips_rts_prebuild(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=True,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    called = {"rts": 0}

    def _ensure_all_rts_images():
        called["rts"] += 1
        return {
            "jvm": "crsbench-rts-jvm:latest",
            "c": "crsbench-rts-c:latest",
        }

    monkeypatch.setattr(
        "crsbench.builder.rts.dockerfile_swap.ensure_all_rts_images",
        _ensure_all_rts_images,
    )
    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_all_rts_images",
        _ensure_all_rts_images,
        raising=False,
    )

    assert run_prepare(args) == 0
    assert called["rts"] == 0


def test_run_prepare_rts_prebuild_failure_returns_nonzero(monkeypatch) -> None:
    args = argparse.Namespace(
        coverage=False,
        skip_base_images=False,
        build_base_images=False,
    )

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    calls: list[object] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    def _ensure_all_rts_images():
        calls.append("rts")
        raise RuntimeError("rts prebuild failed")

    monkeypatch.setattr(
        "crsbench.builder.rts.dockerfile_swap.ensure_all_rts_images",
        _ensure_all_rts_images,
    )
    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_all_rts_images",
        _ensure_all_rts_images,
        raising=False,
    )

    assert run_prepare(args) == 1
    assert calls[-1] == "rts"
