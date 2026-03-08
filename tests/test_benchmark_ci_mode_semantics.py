"""Focused mode-semantics tests that are not under skipped integration classes."""

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from crsbench.benchmark_ci.cli import add_ci_subparser
from crsbench.benchmark_ci.models import CheckResult, CheckStatus


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    add_ci_subparser(subs)
    return parser


def _full_mode_args() -> argparse.Namespace:
    return argparse.Namespace(all=True, mode="full")


def test_retry_defaults_to_full_mode_for_compatibility() -> None:
    parser = _make_parser()
    args = parser.parse_args(["ci", "retry", "--csv", "/tmp/summary.csv"])
    assert args.mode == "full"


@pytest.mark.parametrize(
    ("module_path", "resolve_name", "run_name"),
    [
        (
            "crsbench.benchmark_ci.cli.commands.pov_cmd",
            "resolve_benchmark_paths",
            "run_pov",
        ),
        (
            "crsbench.benchmark_ci.cli.commands.patch_cmd",
            "resolve_benchmark_paths",
            "run_patch",
        ),
        (
            "crsbench.benchmark_ci.cli.commands.coverage_cmd",
            "resolve_benchmark_paths",
            "run_coverage",
        ),
    ],
)
def test_full_mode_disables_inc_build_for_ci_commands(
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    resolve_name: str,
    run_name: str,
) -> None:
    module = __import__(module_path, fromlist=["_placeholder"])
    all_cmd = __import__(
        "crsbench.benchmark_ci.cli.commands.all_cmd", fromlist=["_placeholder"]
    )
    captured: dict[str, bool] = {}

    def fake_build_dag(*_args, **kwargs):
        captured["use_inc_build"] = kwargs["use_inc_build"]
        raise RuntimeError("stop-after-build-dag")

    monkeypatch.setattr(module, resolve_name, lambda **_kwargs: [Path("/tmp/bench1")])
    monkeypatch.setattr(all_cmd, "_build_dag", fake_build_dag)

    run_func = getattr(module, run_name)
    with pytest.raises(RuntimeError, match="stop-after-build-dag"):
        run_func(_full_mode_args())

    assert captured["use_inc_build"] is False


def test_all_full_mode_disables_inc_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crsbench.benchmark_ci.cli.commands import all_cmd

    captured: dict[str, bool] = {}

    def fake_build_dag(*_args, **kwargs):
        captured["use_inc_build"] = kwargs["use_inc_build"]
        raise RuntimeError("stop-after-build-dag")

    monkeypatch.setattr(
        all_cmd, "resolve_benchmark_paths", lambda **_kwargs: [Path("/tmp/bench1")]
    )
    monkeypatch.setattr(
        all_cmd,
        "validate_format",
        lambda *_args, **_kwargs: SimpleNamespace(
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=0.0)
        ),
    )
    monkeypatch.setattr(all_cmd, "_build_dag", fake_build_dag)

    with pytest.raises(RuntimeError, match="stop-after-build-dag"):
        all_cmd.run_all(_full_mode_args())

    assert captured["use_inc_build"] is False


def test_build_full_mode_disables_inc_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from crsbench.benchmark_ci.cli.commands import build_cmd
    from crsbench.executor import variant_planner

    captured: dict[str, bool] = {}

    class FakePlanner:
        def __init__(self, *args, **kwargs):
            pass

        def plan_all_builds(self, *_args, **kwargs):
            captured["use_inc_build"] = kwargs["use_inc_build"]
            raise RuntimeError("stop-after-plan")

    monkeypatch.setattr(
        build_cmd, "resolve_benchmark_paths", lambda **_kwargs: [Path("/tmp/bench1")]
    )
    monkeypatch.setattr(variant_planner, "VariantPlanner", FakePlanner)

    with pytest.raises(RuntimeError, match="stop-after-plan"):
        build_cmd.run_build(_full_mode_args())

    assert captured["use_inc_build"] is False


def test_rts_command_returns_skip_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from crsbench.benchmark_ci.cli.commands import rts_cmd

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        rts_cmd, "resolve_benchmark_paths", lambda **_kwargs: [Path("/tmp/bench1")]
    )
    monkeypatch.setattr(
        rts_cmd,
        "print_results_table",
        lambda summary, **_kwargs: captured.setdefault("summary", summary),
    )

    result = rts_cmd.run_rts(_full_mode_args())
    assert result == 0
    assert captured["summary"].results[0].total_status == CheckStatus.SKIP
