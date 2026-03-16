"""Tests for the timeline coverage backend session."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from crsbench.evaluation.coverage.backend import (
    CoverageRunResult,
    DockerCoverageSession,
    JazzerWarmCoverageSession,
    ShardedCoverageSession,
    UniAFLCoverageSession,
)
from crsbench.evaluation.coverage.strategy import (
    JaCoCoLineStrategy,
    LLVMCovLineStrategy,
)


class _DummyProc:
    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        del timeout
        return 0

    def kill(self):
        return None


def test_docker_session_prepares_matching_llvm_tools_for_native_targets(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "container-id\n"
            stderr = ""

        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        session = DockerCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:
        cp_calls = [call for call in calls if call[:2] == ["docker", "cp"]]
        assert cp_calls
        assert any("/usr/local/bin/llvm-profdata" in call[2] for call in cp_calls)
        assert any("/usr/local/bin/llvm-cov" in call[2] for call in cp_calls)
        assert (session.toolchain_dir / "bin").is_dir()
    finally:
        session.close()


def test_docker_session_uses_matching_llvm_toolchain_in_coverage_script(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = DockerCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda _path: {},
        )

    try:
        observed: list[str] = []

        def fake_exec(script: str, *, timeout: int = 300):
            del timeout
            observed.append(script)
            return "", "", 0, False

        session._exec = fake_exec  # type: ignore[method-assign]
        session._run_coverage_script("seed-1", timeout=300)

        assert observed
        assert "PATH=/workspace/toolchain/bin:$PATH" in observed[0]
        assert "FUZZING_LANGUAGE=c++" in observed[0]
    finally:
        session.close()


def test_docker_session_uses_jacoco_xml_for_jvm_batch_totals(tmp_path: Path) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "seed").write_bytes(b"a")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = DockerCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="FuzzTarget",
            language="jvm",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=None,
            parse_summary=lambda path: {"summary_path": path.name},
        )

    try:

        def fake_run_coverage(output_subdir: str, *, timeout: int):
            del timeout
            run_output = session.outputs_dir / output_subdir / "report" / "linux"
            run_output.mkdir(parents=True, exist_ok=True)
            (run_output / "jacoco.xml").write_text("<report/>")
            return "", "", 0, False

        session._run_coverage_script = fake_run_coverage  # type: ignore[method-assign]

        def fake_exec(_script: str, *, timeout: int = 300):
            del timeout
            return "", "", 0, False

        session._exec = fake_exec  # type: ignore[method-assign]

        result = session.collect_batch_totals(corpus_dir)

        assert result == {"summary_path": "jacoco.xml"}
    finally:
        session.close()


def test_docker_session_collect_single_persists_raw_outputs(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"abc")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        session = DockerCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {
                "main": {"src": "/src/main.c", "lines": [1, 2]}
            },
            parse_textcov_output=None,
            parse_summary=lambda _path: {"lines_total": 10},
        )

    try:

        def fake_run_coverage(output_subdir: str, *, timeout: int):
            del timeout
            run_output = session.outputs_dir / output_subdir
            (run_output / "dumps").mkdir(parents=True, exist_ok=True)
            (run_output / "dumps" / "fuzz_target.profdata").write_text("profdata")
            (run_output / "fuzzer_stats").mkdir(parents=True, exist_ok=True)
            (run_output / "fuzzer_stats" / "fuzz_target_error.log").write_text("boom")
            return "stdout", "stderr", 1, False

        session._run_coverage_script = fake_run_coverage  # type: ignore[method-assign]
        session._export_llvm_json = lambda corpus_hash, _run_output_dir: (  # type: ignore[method-assign]
            {"main": {"src": "/src/main.c", "lines": [1, 2]}},
            session.raw_dir / f"{corpus_hash}.llvm-export.json",
        )

        result = session.collect_single(seed)

        assert result.crashed is True
        assert result.raw_cov_path is not None
        assert result.raw_cov_path.exists()
        assert result.crash_log_path is not None
        assert result.crash_log_path.exists()
        assert json.loads(result.raw_cov_path.read_text()) == {
            "main": {"src": "/src/main.c", "lines": [1, 2]}
        }
        assert (session.raw_dir / f"{result.raw_cov_path.stem}.stdout.log").exists()
        assert (session.raw_dir / f"{result.raw_cov_path.stem}.stderr.log").exists()
    finally:
        session.close()


def test_docker_session_collect_single_prefers_native_textcov_reports(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"abc")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        session = DockerCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="fuzz_target",
            language="c",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_textcov_output=lambda path: {
                "from_textcov": {"src": str(path), "lines": [7, 8]}
            },
            parse_summary=lambda _path: {"lines_total": 10},
        )

    try:

        def fake_run_coverage(output_subdir: str, *, timeout: int):
            del timeout
            run_output = session.outputs_dir / output_subdir
            (run_output / "textcov_reports").mkdir(parents=True, exist_ok=True)
            (run_output / "textcov_reports" / "fuzz_target.covreport").write_text(
                "main:\n  7|      1|line\n"
            )
            return "stdout", "stderr", 0, False

        session._run_coverage_script = fake_run_coverage  # type: ignore[method-assign]
        session._export_llvm_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"from_export": {"src": "/src/main.c", "lines": [1]}},
            session.raw_dir / "unexpected.llvm-export.json",
        )

        result = session.collect_single(seed)

        assert result.coverage_data == {
            "from_textcov": {
                "src": str(
                    session.outputs_dir
                    / result.raw_cov_path.stem
                    / "textcov_reports"
                    / "fuzz_target.covreport"
                ),
                "lines": [7, 8],
            }
        }
        assert (session.raw_dir / f"{result.raw_cov_path.stem}.covreport").exists()
    finally:
        session.close()


def test_jvm_warm_session_processes_multiple_inputs_without_restart(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    (build_output_dir / "ExpanderFuzzer").write_text("#!/bin/sh\n")
    seed1 = tmp_path / "seed1.bin"
    seed2 = tmp_path / "seed2.bin"
    seed1.write_bytes(b"a")
    seed2.write_bytes(b"b")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = JazzerWarmCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="ExpanderFuzzer",
            language="jvm",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_summary=lambda _path: {},
        )

    try:

        def fake_collect(corpus_hash: str, corpus_file: Path) -> CoverageRunResult:
            raw_cov_path = session.raw_dir / f"{corpus_hash}.cov"
            raw_cov_path.write_text(
                json.dumps(
                    {"input": corpus_file.name, "main": {"src": "A.java", "lines": [1]}}
                )
            )
            return CoverageRunResult(
                coverage_data={"main": {"src": "A.java", "lines": [1]}},
                raw_cov_path=raw_cov_path,
            )

        session._collect_single_from_worker = fake_collect  # type: ignore[method-assign]

        result1 = session.collect_single(seed1)
        result2 = session.collect_single(seed2)

        assert result1.raw_cov_path != result2.raw_cov_path
        assert session.worker_start_count == 1
    finally:
        session.close()


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
        with patch("subprocess.Popen", return_value=_DummyProc()):
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
        with patch("subprocess.Popen", return_value=_DummyProc()):
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
        with patch("subprocess.Popen", return_value=_DummyProc()):
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


def test_jvm_warm_session_starts_worker_from_build_output_script(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    harness_wrapper = build_output_dir / "ExpanderFuzzer"
    harness_wrapper.write_text("#!/bin/sh\n")
    recorded_cmds: list[list[str]] = []

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        session = JazzerWarmCoverageSession(
            project_name="proj-cov-delta-coverage",
            harness_name="ExpanderFuzzer",
            language="jvm",
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=lambda _data: {},
            parse_summary=lambda _path: {},
        )

    try:
        with patch(
            "subprocess.Popen",
            side_effect=lambda cmd, **_kwargs: recorded_cmds.append(cmd)
            or _DummyProc(),
        ):  # type: ignore[arg-type]
            session._ensure_worker_started()
        assert recorded_cmds
        assert recorded_cmds[0][:3] == ["docker", "exec", "-i"]
        joined = " ".join(recorded_cmds[0])
        assert "/out/ExpanderFuzzer" in joined
        assert "--crsbench_warm_coverage" in joined
        assert "--crsbench_request_dir=/workspace/worker-requests" in joined
        assert "--crsbench_result_dir=/workspace/worker-results" in joined
        assert session.worker_start_count == 1
    finally:
        session.close()


def test_uniafl_session_collect_many_uses_long_lived_worker_service(
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
        capture_output = kwargs.get("capture_output", False)
        text = kwargs.get("text", False)
        timeout = kwargs.get("timeout")
        del capture_output, text, timeout

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if cmd[:2] == ["docker", "run"]:
            Result.stdout = "container-id\n"
            return Result()
        if cmd[:2] == ["docker", "exec"] and "prepare" in cmd:
            return Result()
        if cmd[:2] == ["docker", "exec"] and "run" in cmd:
            output_root = Path(cmd[cmd.index("/workspace/outputs")])
            workspace = Path(cmd[cmd.index("/workspace/outputs")]).parent
            host_output_root = output_root
            del workspace
            return Result()
        if cmd[:3] == ["docker", "rm", "-f"]:
            return Result()
        return Result()

    with patch("subprocess.run", side_effect=fake_run):
        with patch("subprocess.Popen", return_value=_DummyProc()):
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
            recorded_hashes: list[str] = []

            def fake_wait_for_result(corpus_hash: str) -> CoverageRunResult:
                recorded_hashes.append(corpus_hash)
                raw_cov_path = session.raw_dir / f"{corpus_hash}.cov"
                raw_cov_path.write_text(
                    json.dumps({"main": {"src": "src.c", "lines": [1]}})
                )
                return CoverageRunResult(
                    coverage_data={"main": {"src": "src.c", "lines": [1]}},
                    raw_cov_path=raw_cov_path,
                )

            session._wait_for_result = fake_wait_for_result  # type: ignore[method-assign]
            results = session.collect_many([seed1, seed2])
            assert set(results) == {seed1, seed2}
            assert recorded_hashes == [
                hashlib.sha256(seed1.read_bytes()).hexdigest()[:16],
                hashlib.sha256(seed2.read_bytes()).hexdigest()[:16],
            ]
        finally:
            session.close()


def test_uniafl_session_starts_worker_service_with_request_and_result_dirs(
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
        with patch(
            "subprocess.Popen",
            side_effect=lambda cmd, **_kwargs: recorded_cmds.append(cmd)
            or _DummyProc(),
        ):  # type: ignore[arg-type]
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
        assert recorded_cmds
        joined = " ".join(recorded_cmds[0])
        assert recorded_cmds[0][:3] == ["docker", "exec", "-i"]
        assert "python3 /workspace/crsbench_cov_worker.py serve fuzz_target" in joined
        assert "/workspace/requests" in joined
        assert "/workspace/outputs" in joined
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
        with patch("subprocess.Popen", return_value=_DummyProc()):
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
        with patch("subprocess.Popen", return_value=_DummyProc()):
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


def test_jvm_warm_session_collect_single_consumes_result_files(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "coverage"
    build_output_dir = tmp_path / "build-out"
    build_output_dir.mkdir()
    (build_output_dir / "ExpanderFuzzer").write_text("#!/bin/sh\n")
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"a")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "container-id\n"
        mock_run.return_value.stderr = ""
        with patch("subprocess.Popen", return_value=_DummyProc()):
            session = JazzerWarmCoverageSession(
                project_name="proj-cov-delta-coverage",
                harness_name="ExpanderFuzzer",
                language="jvm",
                build_output_dir=build_output_dir,
                output_dir=output_dir,
                parse_single_output=lambda _data: {},
                parse_summary=lambda _path: {},
            )

    try:

        def fake_wait(corpus_hash: str):
            cov_path = session.results_dir / f"{corpus_hash}.cov"
            status_path = session.results_dir / f"{corpus_hash}.status.json"
            cov_path.write_text(
                json.dumps({"A.java": {"src": "A.java", "lines": [1, 2]}})
            )
            status_path.write_text(json.dumps({"crashed": True}))
            crash_path = session.results_dir / f"{corpus_hash}.crash.log"
            crash_path.write_text("boom")
            return cov_path, status_path, crash_path

        session._wait_for_worker_artifacts = fake_wait  # type: ignore[method-assign]
        result = session.collect_single(seed)

        assert result.coverage_data == {"A.java": {"src": "A.java", "lines": [1, 2]}}
        assert result.crashed is True
        assert result.raw_cov_path is not None
        assert result.raw_cov_path.exists()
        assert result.crash_log_path is not None
        assert result.crash_log_path.exists()
        assert session.worker_start_count == 1
    finally:
        session.close()


def test_uniafl_worker_script_exposes_seed_directory_mode() -> None:
    from crsbench.evaluation.coverage import backend

    script = backend._UNIAFL_COVERAGE_WORKER_SCRIPT
    assert "def _run_dir(" in script
    assert 'if command == "run-dir":' in script


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
        assert session.collect_batch_totals(tmp_path) == {
            "corpus_dir": tmp_path.name,
            "worker": "left",
        }
    finally:
        session.close()
        assert left.closed is True
        assert right.closed is True
