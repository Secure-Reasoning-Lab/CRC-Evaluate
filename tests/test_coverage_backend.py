"""Tests for the timeline coverage backend session."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from crsbench.evaluation.coverage.backend import (
    CoverageRunResult,
    ShardedCoverageSession,
    UniAFLCoverageSession,
)
from crsbench.evaluation.coverage.strategy import (
    JaCoCoLineStrategy,
    LLVMCovLineStrategy,
)


def test_native_strategy_opens_uniafl_session(tmp_path: Path) -> None:
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").touch()
    build_out = (
        oss_fuzz / "build" / "out" / "proj-cov-delta-coverage" / ".crsbench-repo"
    )
    build_out.mkdir(parents=True)
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    (benchmark / "project.yaml").write_text("language: c\n")
    (benchmark / ".aixcc").mkdir()

    strategy = LLVMCovLineStrategy(
        oss_fuzz,
        "proj-cov-delta-coverage",
        "c",
        benchmark_path=benchmark,
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = strategy.open_session("fuzz_target", output_dir=tmp_path / "coverage")
    try:
        assert type(session).__name__ == "UniAFLCoverageSession"
    finally:
        session.close()


def test_jvm_strategy_opens_uniafl_session(tmp_path: Path) -> None:
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").touch()
    build_out = (
        oss_fuzz / "build" / "out" / "proj-cov-delta-coverage" / ".crsbench-repo"
    )
    build_out.mkdir(parents=True)
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    (benchmark / "project.yaml").write_text("language: jvm\n")
    (benchmark / ".aixcc").mkdir()

    strategy = JaCoCoLineStrategy(
        oss_fuzz,
        "proj-cov-delta-coverage",
        "jvm",
        benchmark_path=benchmark,
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = strategy.open_session("FuzzTarget", output_dir=tmp_path / "coverage")
    try:
        assert type(session).__name__ == "UniAFLCoverageSession"
    finally:
        session.close()


def test_uniafl_session_uses_uniafl_crs_runtime_image_for_jvm(tmp_path: Path) -> None:
    from crsbench.prepare.uniafl_backend import default_uniafl_runtime_image

    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: jvm\n")
    (benchmark_dir / ".aixcc").mkdir()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="ExpanderFuzzer",
            language="jvm",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )
    try:
        assert session.runtime_image == default_uniafl_runtime_image("jvm")
    finally:
        session.close()


def test_uniafl_session_starts_container_with_run_fuzzer_env(tmp_path: Path) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: jvm\n")
    (benchmark_dir / ".aixcc").mkdir()

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen_cmds.append(cmd)

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="ExpanderFuzzer",
            language="jvm",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )
    try:
        docker_run = next(cmd for cmd in seen_cmds if cmd[:2] == ["docker", "run"])
        joined = " ".join(docker_run)
        assert "--privileged" in docker_run
        assert "--shm-size=2g" in docker_run
        assert "FUZZING_ENGINE=libfuzzer" in joined
        assert "RUN_FUZZER_MODE=batch" in joined
        assert "SANITIZER=address" in joined
        assert "POV_DIR=/povs" in joined
        assert "CORPUS_DIR=/corpus" in joined
        assert "CRS_DATA_DIR=/crs-data" in joined
        assert "SEED_SHARE_DIR=/shared-seeds" in joined
        assert "ln -snf /out/coverage-out /coverage-out" in joined
    finally:
        session.close()


def test_uniafl_session_pins_container_to_requested_cpu(tmp_path: Path) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: c\n")
    (benchmark_dir / ".aixcc").mkdir()

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen_cmds.append(cmd)

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
            cpu_set="7",
            session_label="worker-0",
        )
    try:
        docker_run = next(cmd for cmd in seen_cmds if cmd[:2] == ["docker", "run"])
        assert "--cpuset-cpus" in docker_run
        assert docker_run[docker_run.index("--cpuset-cpus") + 1] == "7"
    finally:
        session.close()


def test_uniafl_session_uses_isolated_runtime_out_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: c\n")
    (benchmark_dir / ".aixcc").mkdir()
    (build_output_dir / "fuzz_target").write_text("#!/bin/sh\n")

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen_cmds.append(cmd)

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )
    try:
        docker_run = next(cmd for cmd in seen_cmds if cmd[:2] == ["docker", "run"])
        out_mount = next(
            mount for mount in docker_run if mount.endswith(":/out")
        ).removesuffix(":/out")
        src_mount = next(
            mount for mount in docker_run if mount.endswith(":/src")
        ).removesuffix(":/src")
        # After the workspace split, /src and /out mount the originals directly
        # (no more copies) to avoid multi-GB /tmp bloat.
        assert Path(out_mount) == build_output_dir.resolve()
        assert Path(out_mount).is_dir()
        assert (Path(out_mount) / "fuzz_target").exists()
        assert Path(src_mount) == benchmark_dir.resolve()
        assert Path(src_mount).is_dir()
        assert (Path(src_mount) / ".aixcc").is_dir()
    finally:
        session.close()


def test_uniafl_session_collect_many_runs_seed_shard_through_run_dir(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: c\n")
    (benchmark_dir / ".aixcc").mkdir()
    seed1 = tmp_path / "seed1"
    seed2 = tmp_path / "seed2"
    seed1.write_bytes(b"a")
    seed2.write_bytes(b"b")

    def fake_run(cmd, **kwargs):
        del kwargs

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if cmd[:2] == ["docker", "run"]:
            Result.stdout = "container-id\n"
            return Result()
        if cmd[:2] == ["docker", "exec"] and "prepare" in cmd:
            return Result()
        if cmd[:2] == ["docker", "exec"] and "run-dir" in cmd:
            host_seed_root = session.workspace / Path(cmd[-2]).relative_to("/workspace")
            host_output_root = session.workspace / Path(cmd[-1]).relative_to(
                "/workspace"
            )
            host_output_root.mkdir(parents=True, exist_ok=True)
            for staged_seed in sorted(host_seed_root.iterdir()):
                seed_hash = staged_seed.name
                (host_output_root / f"{seed_hash}.cov").write_text(
                    json.dumps({"main": {"src": "src.c", "lines": [1]}})
                )
                (host_output_root / f"{seed_hash}.status.json").write_text(
                    json.dumps({"crashed": False})
                )
            return Result()
        if cmd[:3] == ["docker", "rm", "-f"]:
            return Result()
        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

        try:
            results = session.collect_many([seed1, seed2])
            assert set(results) == {seed1, seed2}
            assert sorted(result.raw_cov_path.stem for result in results.values()) == [
                hashlib.sha256(seed2.read_bytes()).hexdigest()[:16],
                hashlib.sha256(seed1.read_bytes()).hexdigest()[:16],
            ]
            assert all(result.coverage_data for result in results.values())
            assert list(session.runs_dir.iterdir()) == []
        finally:
            session.close()


def test_uniafl_session_invokes_run_dir_with_mounted_workspace_paths(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: jvm\n")
    (benchmark_dir / ".aixcc").mkdir()

    recorded_cmds: list[list[str]] = []

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="jvm",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )
        session._docker_exec = lambda args, **_kwargs: (  # type: ignore[method-assign]
            recorded_cmds.append(args) or mock_run.return_value
        )

    try:
        seed = tmp_path / "seed"
        seed.write_bytes(b"a")
        with patch.object(
            session,
            "_load_result_from_output_root",
            return_value=CoverageRunResult(coverage_data={}),
        ):
            session.collect_many([seed])

        run_dir_cmd = next(
            cmd
            for cmd in recorded_cmds
            if cmd[:3]
            == [
                "python3",
                "/workspace/crsbench_cov_worker.py",
                "run-dir",
            ]
        )
        assert run_dir_cmd[3] == "fuzz_target"
        assert run_dir_cmd[4].startswith("/workspace/runs/")
        assert run_dir_cmd[5].startswith("/workspace/runs/")
    finally:
        session.close()


def test_uniafl_session_batch_totals_match_given_fuzzer_summary_logic(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: c\n")
    (benchmark_dir / ".aixcc").mkdir()
    src_a = tmp_path / "a.c"
    src_b = tmp_path / "b.c"
    src_a.write_text("a\nb\nc\n")
    src_b.write_text("1\n2\n")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:
        session._collected_results = {
            "a": CoverageRunResult(
                coverage_data={
                    "func_a": {"src": str(src_a), "lines": [1, 3]},
                    "func_b": {"src": str(src_b), "lines": [2]},
                },
                raw_cov_path=output_dir / "raw" / "a.cov",
            )
        }
        totals = session.collect_batch_totals(tmp_path)
        assert totals == {
            "lines_covered": 3,
            "lines_total": 5,
            "lines_percent": 60.0,
            "functions_covered": 2,
            "functions_total": 0,
        }
    finally:
        session.close()


def test_uniafl_session_resets_container_before_cleaning_timed_out_batch(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: jvm\n")
    (benchmark_dir / ".aixcc").mkdir()
    seed = tmp_path / "seed"
    seed.write_bytes(b"seed")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="jvm",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:
        session._docker_exec = Mock(  # type: ignore[method-assign]
            side_effect=subprocess.TimeoutExpired(
                cmd=["docker", "exec"],
                timeout=1,
            )
        )
        session._remove_container = Mock()  # type: ignore[method-assign]
        session._start_container = Mock()  # type: ignore[method-assign]
        session._prepare_harness = Mock()  # type: ignore[method-assign]

        with pytest.raises(subprocess.TimeoutExpired):
            session.collect_many([seed])

        assert session._remove_container.call_count == 1
        assert session._start_container.call_count == 1
        assert session._prepare_harness.call_count == 1
        assert list(session.runs_dir.iterdir()) == []
    finally:
        session.close()


def test_uniafl_session_discards_partial_batch_results_after_reset(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = build_output_dir / ".crsbench-repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir(parents=True)
    (benchmark_dir / "project.yaml").write_text("language: c\n")
    (benchmark_dir / ".aixcc").mkdir()
    seed1 = tmp_path / "seed1"
    seed2 = tmp_path / "seed2"
    seed1.write_bytes(b"a")
    seed2.write_bytes(b"b")

    def fake_run(cmd, **kwargs):
        del cmd, kwargs

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:

        def fake_exec(args, **_kwargs):
            if args[:3] != ["python3", "/workspace/crsbench_cov_worker.py", "run-dir"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            host_seed_root = session.workspace / Path(args[4]).relative_to("/workspace")
            host_output_root = session.workspace / Path(args[5]).relative_to(
                "/workspace"
            )
            host_output_root.mkdir(parents=True, exist_ok=True)
            first_seed = sorted(host_seed_root.iterdir())[0]
            (host_output_root / f"{first_seed.name}.cov").write_text(
                json.dumps({"main": {"src": "src.c", "lines": [1]}})
            )
            (host_output_root / f"{first_seed.name}.status.json").write_text(
                json.dumps({"crashed": False})
            )
            return subprocess.CompletedProcess(args, 0, "", "")

        session._docker_exec = fake_exec  # type: ignore[method-assign]
        session._remove_container = Mock()  # type: ignore[method-assign]
        session._start_container = Mock()  # type: ignore[method-assign]
        session._prepare_harness = Mock()  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="missing status"):
            session.collect_many([seed1, seed2])

        assert session._collected_results == {}
        assert session._remove_container.call_count == 1
        assert session._start_container.call_count == 1
        assert session._prepare_harness.call_count == 1
    finally:
        session.close()


def test_uniafl_session_normalizes_container_source_paths(tmp_path: Path) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    benchmark_dir = tmp_path / "benchmark"
    repo_dir = tmp_path / "repo"
    benchmark_dir.mkdir()
    build_output_dir.mkdir()
    repo_dir.mkdir()
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "A.java").write_text("line1\nline2\n")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = UniAFLCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="ExpanderFuzzer",
            language="jvm",
            benchmark_path=benchmark_dir,
            source_repo_dir=repo_dir,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:
        normalized = session._normalize_coverage_data(  # type: ignore[attr-defined]
            {
                "A.m()V": {
                    "src": "/src/repo/src/A.java",
                    "lines": [1, 2],
                }
            }
        )
        assert normalized == {
            "A.m()V": {
                "src": str(repo_dir / "src" / "A.java"),
                "lines": [1, 2],
            }
        }
    finally:
        session.close()


def test_uniafl_worker_script_exposes_seed_directory_mode() -> None:
    from crsbench.evaluation.coverage import backend

    script = backend._UNIAFL_COVERAGE_WORKER_SCRIPT
    assert "def _run_dir(" in script
    assert 'if command == "run-dir":' in script
    assert 'if command == "serve":' not in script
    assert 'if command == "run":' not in script


def test_uniafl_worker_script_falls_back_when_generated_config_is_empty() -> None:
    from crsbench.evaluation.coverage import backend

    script = backend._UNIAFL_COVERAGE_WORKER_SCRIPT
    assert "yaml.safe_load(generated_config.read_text())" in script
    assert "configured_harnesses = {" in script
    assert "harness_name not in configured_harnesses" in script
    assert "generated_config.unlink()" in script
    assert "cp = init_cp_in_runner()" in script


def test_sharded_session_collect_many_merges_parallel_shards(tmp_path: Path) -> None:
    seed_paths: list[Path] = []
    for index in range(32):
        path = tmp_path / f"seed-{index}"
        path.write_bytes(f"payload-{index}".encode())
        seed_paths.append(path)

    class _FakeSession:
        def __init__(self, name: str):
            self.name = name
            self.closed = False
            self.seen: list[Path] = []

        def collect_many(
            self, corpus_files: list[Path]
        ) -> dict[Path, CoverageRunResult]:
            self.seen.extend(corpus_files)
            return {
                path: CoverageRunResult(
                    coverage_data={
                        self.name: {"src": self.name, "lines": [len(path.name)]}
                    }
                )
                for path in corpus_files
            }

        def collect_batch_totals(self, corpus_dir: Path) -> dict:
            return {"corpus_dir": corpus_dir.name, "worker": self.name}

        def close(self) -> None:
            self.closed = True

    left = _FakeSession("left")
    right = _FakeSession("right")
    session = ShardedCoverageSession([left, right])

    try:
        results = session.collect_many(seed_paths)
        assert set(results) == set(seed_paths)
        assert left.seen
        assert right.seen
        assert set(left.seen).isdisjoint(set(right.seen))
    finally:
        session.close()
        assert left.closed is True
        assert right.closed is True


def test_sharded_session_collect_many_matches_collect_single_routing(
    tmp_path: Path,
) -> None:
    seed_paths: list[Path] = []
    for index in range(12):
        path = tmp_path / f"seed-{index}"
        path.write_bytes(f"payload-{index}".encode())
        seed_paths.append(path)

    class _FakeSession:
        def __init__(self, name: str):
            self.name = name
            self.seen: list[Path] = []

        def collect_single(self, corpus_file: Path) -> CoverageRunResult:
            self.seen.append(corpus_file)
            return CoverageRunResult(
                coverage_data={self.name: {"src": self.name, "lines": [1]}}
            )

        def collect_many(
            self, corpus_files: list[Path]
        ) -> dict[Path, CoverageRunResult]:
            self.seen.extend(corpus_files)
            return {
                path: CoverageRunResult(
                    coverage_data={self.name: {"src": self.name, "lines": [1]}}
                )
                for path in corpus_files
            }

        def collect_batch_totals(self, corpus_dir: Path) -> dict:
            del corpus_dir
            return {}

        def close(self) -> None:
            return None

    sessions = [_FakeSession("left"), _FakeSession("right"), _FakeSession("third")]
    sharded = ShardedCoverageSession(sessions)

    results = sharded.collect_many(seed_paths)

    assert set(results) == set(seed_paths)
    expected_session_files = {index: [] for index in range(len(sessions))}
    for seed_path in seed_paths:
        expected_session_files[sharded._session_index_for(seed_path)].append(seed_path)

    for index, session in enumerate(sessions):
        assert session.seen == expected_session_files[index]


def test_sharded_session_collect_batch_totals_aggregates_all_shards(
    tmp_path: Path,
) -> None:
    src_left = tmp_path / "left.py"
    src_left.write_text("a\nb\nc\n")
    src_right = tmp_path / "right.py"
    src_right.write_text("a\nb\nc\nd\n")

    class _FakeSession:
        def __init__(self, name: str, coverage_data: dict[str, dict]):
            self.name = name
            self._collected_results = {
                f"{name}-seed": CoverageRunResult(coverage_data=coverage_data)
            }

        def collect_many(
            self, corpus_files: list[Path]
        ) -> dict[Path, CoverageRunResult]:
            del corpus_files
            return {}

        def collect_batch_totals(self, corpus_dir: Path) -> dict:
            del corpus_dir
            return {
                "lines_covered": 0,
                "lines_total": 0,
                "lines_percent": 0.0,
                "functions_covered": 0,
                "functions_total": 0,
            }

        def close(self) -> None:
            return None

    left = _FakeSession(
        "left",
        {"func_left": {"src": str(src_left), "lines": [1, 2]}},
    )
    right = _FakeSession(
        "right",
        {"func_right": {"src": str(src_right), "lines": [2, 4]}},
    )
    session = ShardedCoverageSession([left, right])

    totals = session.collect_batch_totals(tmp_path)

    assert totals == {
        "lines_covered": 4,
        "lines_total": 7,
        "lines_percent": (4 / 7) * 100.0,
        "functions_covered": 2,
        "functions_total": 0,
    }


def test_sharded_session_collect_batch_totals_replaces_partial_fallback_from_first_shard(
    tmp_path: Path,
) -> None:
    src_left = tmp_path / "left.py"
    src_left.write_text("a\nb\n")
    src_right = tmp_path / "right.py"
    src_right.write_text("a\nb\nc\n")

    class _FakeSession:
        def __init__(
            self,
            *,
            coverage_data: dict[str, dict],
            totals: dict,
            approximate: bool = False,
        ) -> None:
            self._collected_results = {
                "seed": CoverageRunResult(coverage_data=coverage_data)
            }
            self._totals = totals
            self._last_batch_totals_approximate = approximate

        def collect_many(
            self, corpus_files: list[Path]
        ) -> dict[Path, CoverageRunResult]:
            del corpus_files
            return {}

        def collect_batch_totals(self, corpus_dir: Path) -> dict:
            del corpus_dir
            return dict(self._totals)

        def close(self) -> None:
            return None

    left = _FakeSession(
        coverage_data={"func_left": {"src": str(src_left), "lines": [1]}},
        totals={
            "lines_covered": 1,
            "lines_total": 2,
            "lines_percent": 50.0,
            "functions_covered": 1,
            "functions_total": 0,
        },
        approximate=True,
    )
    right = _FakeSession(
        coverage_data={"func_right": {"src": str(src_right), "lines": [2, 3]}},
        totals={
            "lines_covered": 0,
            "lines_total": 0,
            "lines_percent": 0.0,
            "functions_covered": 0,
            "functions_total": 0,
        },
    )
    session = ShardedCoverageSession([left, right])

    totals = session.collect_batch_totals(tmp_path)

    assert totals == {
        "lines_covered": 3,
        "lines_total": 5,
        "lines_percent": (3 / 5) * 100.0,
        "functions_covered": 2,
        "functions_total": 0,
    }
