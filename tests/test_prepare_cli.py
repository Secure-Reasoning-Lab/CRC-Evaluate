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


def test_prepare_parses_defaults():
    parser = _parser()
    args = parser.parse_args(["prepare"])
    assert args.command == "prepare"
    assert args.skip_base_images is False
    assert args.build_base_images is False


def test_prepare_parses_flags():
    parser = _parser()
    args = parser.parse_args(["prepare", "--skip-base-images", "--build-base-images"])
    assert args.command == "prepare"
    assert args.skip_base_images is True
    assert args.build_base_images is True


def test_run_prepare_skip_base_images(monkeypatch):
    args = argparse.Namespace(skip_base_images=True, build_base_images=False)

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    called = {"run": 0}

    def _unexpected_run(*_a, **_kw):
        called["run"] += 1
        raise AssertionError(
            "subprocess.run should not be called when skipping base images"
        )

    monkeypatch.setattr(subprocess, "run", _unexpected_run)

    rc = run_prepare(args)
    assert rc == 0
    assert called["run"] == 0


def test_run_prepare_conflicting_flags_returns_2(monkeypatch):
    args = argparse.Namespace(skip_base_images=True, build_base_images=True)

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    called = {"run": 0}

    def _unexpected_run(*_a, **_kw):
        called["run"] += 1
        raise AssertionError(
            "subprocess.run should not be called for conflicting flags"
        )

    monkeypatch.setattr(subprocess, "run", _unexpected_run)

    rc = run_prepare(args)
    assert rc == 2
    assert called["run"] == 0


def test_run_prepare_bootstrap_failure_returns_1(monkeypatch):
    args = argparse.Namespace(skip_base_images=False, build_base_images=False)

    def _raise_bootstrap():
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        _raise_bootstrap,
    )

    called = {"run": 0}

    def _unexpected_run(*_a, **_kw):
        called["run"] += 1
        raise AssertionError("subprocess.run should not be called on bootstrap failure")

    monkeypatch.setattr(subprocess, "run", _unexpected_run)

    rc = run_prepare(args)
    assert rc == 1
    assert called["run"] == 0


def test_run_prepare_pull_images_failure(monkeypatch):
    args = argparse.Namespace(skip_base_images=False, build_base_images=False)

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    def _run(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=["x"], returncode=2, stdout="oops", stderr="err"
        )

    monkeypatch.setattr(subprocess, "run", _run)

    rc = run_prepare(args)
    assert rc == 2


def test_run_prepare_with_local_base_image_build(monkeypatch, tmp_path):
    args = argparse.Namespace(skip_base_images=False, build_base_images=True)

    oss_fuzz_root = tmp_path / "oss-fuzz"
    (oss_fuzz_root / "infra" / "base-images").mkdir(parents=True)
    (oss_fuzz_root / "infra" / "base-images" / "all.sh").write_text("#!/bin/bash\n")

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: str(oss_fuzz_root),
    )

    calls: list[list[str]] = []

    def _run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    rc = run_prepare(args)
    assert rc == 0
    assert calls[0] == ["python3", "infra/helper.py", "pull_images"]
    assert calls[1] == ["docker", "pull", "ghcr.io/aixcc-finals/base-builder:v1.3.0"]
    assert calls[2] == ["docker", "pull", "ghcr.io/aixcc-finals/base-runner:v1.3.0"]
    assert calls[3] == [
        "docker",
        "pull",
        "ghcr.io/aixcc-finals/base-builder-jvm:v1.3.0",
    ]
    assert calls[4] == ["bash", str(oss_fuzz_root / "infra" / "base-images" / "all.sh")]


def test_run_prepare_aixcc_pull_failure(monkeypatch):
    args = argparse.Namespace(skip_base_images=False, build_base_images=False)

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: "/tmp/oss-fuzz",
    )

    calls = {"n": 0}

    def _run(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(
                args=["python3", "infra/helper.py", "pull_images"],
                returncode=0,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=["docker", "pull", "ghcr.io/aixcc-finals/base-builder:v1.3.0"],
            returncode=1,
            stdout="",
            stderr="pull failed",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    rc = run_prepare(args)
    assert rc == 1


def test_run_prepare_missing_base_image_build_script_returns_1(monkeypatch, tmp_path):
    args = argparse.Namespace(skip_base_images=False, build_base_images=True)

    oss_fuzz_root = tmp_path / "oss-fuzz"
    oss_fuzz_root.mkdir(parents=True)

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: str(oss_fuzz_root),
    )

    calls = {"n": 0}

    def _run(*_a, **_kw):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args=["x"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)
    rc = run_prepare(args)
    assert rc == 1
    # helper pull + 3 AIXCC pulls; should fail before local build exec
    assert calls["n"] == 4


def test_run_prepare_local_build_failure_propagates_rc(monkeypatch, tmp_path):
    args = argparse.Namespace(skip_base_images=False, build_base_images=True)

    oss_fuzz_root = tmp_path / "oss-fuzz"
    (oss_fuzz_root / "infra" / "base-images").mkdir(parents=True)
    (oss_fuzz_root / "infra" / "base-images" / "all.sh").write_text("#!/bin/bash\n")

    monkeypatch.setattr(
        "crsbench.prepare.cli.ensure_oss_fuzz_root",
        lambda: str(oss_fuzz_root),
    )

    calls = {"n": 0}

    def _run(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] < 5:
            return subprocess.CompletedProcess(
                args=["x"], returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(
            args=["x"], returncode=7, stdout="", stderr="build failed"
        )

    monkeypatch.setattr(subprocess, "run", _run)
    rc = run_prepare(args)
    assert rc == 7
