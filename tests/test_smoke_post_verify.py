import json
from pathlib import Path

from crsbench.benchmark_ci.smoke_post_verify import (
    SmokeVerificationTask,
    discover_smoke_verification_tasks,
    run_smoke_post_verification,
)


def _write_meta(benchmark_path: Path, harness_cpvs: dict[str, list[str]]) -> None:
    aixcc_dir = benchmark_path / ".aixcc"
    aixcc_dir.mkdir(parents=True)
    meta = {"harness_files": []}
    for harness, cpvs in harness_cpvs.items():
        meta["harness_files"].append(
            {
                "name": harness,
                "vulns": [{"vuln_keyword": cpv} for cpv in cpvs],
            }
        )
    (aixcc_dir / "meta.yaml").write_text(json.dumps(meta))


def _write_trial_metadata(trial_dir: Path, benchmark: str, harness: str) -> None:
    trial_dir.mkdir(parents=True)
    (trial_dir / "metadata.json").write_text(
        json.dumps({"benchmark": benchmark, "harness": harness})
    )
    (trial_dir / ".success").touch()


def test_discover_smoke_verification_tasks_uses_trial_metadata_and_benchmarks_root(
    tmp_path: Path,
) -> None:
    benchmarks_root = tmp_path / "benchmarks"
    benchmark_path = benchmarks_root / "demo-bench"
    _write_meta(benchmark_path, {"fuzz_a": ["cpv_0"]})

    experiment_dir = tmp_path / "experiment"
    trial_dir = experiment_dir / "crs" / "demo-bench" / "fuzz_a" / "delta" / "trial-1"
    _write_trial_metadata(trial_dir, "demo-bench", "fuzz_a")

    tasks = discover_smoke_verification_tasks(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite="bugfinding",
    )

    assert tasks == [
        SmokeVerificationTask(
            suite="bugfinding",
            trial_dir=trial_dir,
            benchmark_path=benchmark_path,
            benchmark_name="demo-bench",
            harness="fuzz_a",
        )
    ]


def test_run_smoke_post_verification_bugfinding_uses_verify_and_harness_filter(
    tmp_path: Path,
) -> None:
    benchmarks_root = tmp_path / "benchmarks"
    benchmark_path = benchmarks_root / "demo-bench"
    _write_meta(benchmark_path, {"fuzz_a": ["cpv_0"], "fuzz_b": ["cpv_1"]})

    experiment_dir = tmp_path / "experiment"
    trial_dir = experiment_dir / "crs" / "demo-bench" / "fuzz_a" / "delta" / "trial-1"
    _write_trial_metadata(trial_dir, "demo-bench", "fuzz_a")
    output_povs = trial_dir / "output" / "povs"
    output_povs.mkdir(parents=True)
    (output_povs / "pov_0.blob").write_bytes(b"pov")

    seen_commands: list[list[str]] = []

    def _runner(command: list[str]) -> int:
        seen_commands.append(command)
        result_path = Path(command[command.index("--output") + 1])
        result_path.write_text(
            json.dumps([{"harness": "fuzz_a", "cpv_matched": ["cpv_0"]}])
        )
        return 0

    exit_code = run_smoke_post_verification(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite="bugfinding",
        run_command=_runner,
        runner_prefix=["crsbench"],
    )

    assert exit_code == 0
    assert seen_commands == [
        [
            "crsbench",
            "verify",
            str(benchmark_path),
            "--pov-dir",
            str(output_povs),
            "--harness",
            "fuzz_a",
            "--output",
            str(trial_dir / "smoke_verify_results.json"),
            "--format",
            "json",
        ]
    ]


def test_run_smoke_post_verification_bugfixing_uses_patch_verify_and_input_povs(
    tmp_path: Path,
) -> None:
    benchmarks_root = tmp_path / "benchmarks"
    benchmark_path = benchmarks_root / "demo-bench"
    _write_meta(benchmark_path, {"fuzz_a": ["cpv_0"]})

    experiment_dir = tmp_path / "experiment"
    trial_dir = experiment_dir / "crs" / "demo-bench" / "fuzz_a" / "delta" / "trial-1"
    _write_trial_metadata(trial_dir, "demo-bench", "fuzz_a")
    output_patches = trial_dir / "output" / "patches" / "cpv_0"
    output_patches.mkdir(parents=True)
    (output_patches / "patch.diff").write_text("--- a\n+++ b\n")
    input_povs = trial_dir / "crs-input" / "povs"
    input_povs.mkdir(parents=True)
    (input_povs / "cpv_0.blob").write_bytes(b"pov")

    seen_commands: list[list[str]] = []

    def _runner(command: list[str]) -> int:
        seen_commands.append(command)
        result_path = Path(command[command.index("--output") + 1])
        result_path.write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "pov_id": "cpv_0",
                            "patch_id": "patch",
                            "security_verdict": "PASS",
                        }
                    ]
                }
            )
        )
        return 0

    exit_code = run_smoke_post_verification(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite="bugfixing",
        run_command=_runner,
        runner_prefix=["crsbench"],
    )

    assert exit_code == 0
    assert seen_commands == [
        [
            "crsbench",
            "patch-verify",
            str(benchmark_path),
            "--patch-dir",
            str(trial_dir / "output" / "patches"),
            "--pov-dir",
            str(input_povs),
            "--harness",
            "fuzz_a",
            "--output",
            str(trial_dir / "smoke_patch_verify_results.json"),
            "--format",
            "json",
        ]
    ]


def test_run_smoke_post_verification_rejects_failed_trials_before_running_commands(
    tmp_path: Path,
) -> None:
    benchmarks_root = tmp_path / "benchmarks"
    benchmark_path = benchmarks_root / "demo-bench"
    _write_meta(benchmark_path, {"fuzz_a": ["cpv_0"]})

    experiment_dir = tmp_path / "experiment"
    trial_dir = experiment_dir / "crs" / "demo-bench" / "fuzz_a" / "delta" / "trial-1"
    _write_trial_metadata(trial_dir, "demo-bench", "fuzz_a")
    (trial_dir / ".fail").touch()
    output_povs = trial_dir / "output" / "povs"
    output_povs.mkdir(parents=True)
    (output_povs / "pov_0.blob").write_bytes(b"pov")

    called = False

    def _runner(_command: list[str]) -> int:
        nonlocal called
        called = True
        return 0

    exit_code = run_smoke_post_verification(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite="bugfinding",
        run_command=_runner,
        runner_prefix=["crsbench"],
    )

    assert exit_code == 1
    assert called is False


def test_run_smoke_post_verification_rejects_undrained_patch_verify_marker(
    tmp_path: Path,
) -> None:
    benchmarks_root = tmp_path / "benchmarks"
    benchmark_path = benchmarks_root / "demo-bench"
    _write_meta(benchmark_path, {"fuzz_a": ["cpv_0"]})

    experiment_dir = tmp_path / "experiment"
    trial_dir = experiment_dir / "crs" / "demo-bench" / "fuzz_a" / "delta" / "trial-1"
    _write_trial_metadata(trial_dir, "demo-bench", "fuzz_a")
    (trial_dir / ".verification-undrained.json").write_text(
        json.dumps(
            {
                "verification_kind": "patch",
                "reason": "async_verification_drain_incomplete",
                "expected_jobs": 1,
                "completed_results": 0,
                "missing_results": 1,
            }
        )
    )
    output_patches = trial_dir / "output" / "patches" / "cpv_0"
    output_patches.mkdir(parents=True)
    (output_patches / "patch.diff").write_text("--- a\n+++ b\n")
    input_povs = trial_dir / "crs-input" / "povs"
    input_povs.mkdir(parents=True)
    (input_povs / "cpv_0.blob").write_bytes(b"pov")

    called = False

    def _runner(_command: list[str]) -> int:
        nonlocal called
        called = True
        return 0

    exit_code = run_smoke_post_verification(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite="bugfixing",
        run_command=_runner,
        runner_prefix=["crsbench"],
    )

    assert exit_code == 1
    assert called is False
