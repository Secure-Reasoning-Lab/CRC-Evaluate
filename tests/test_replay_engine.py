import json
from pathlib import Path
from unittest.mock import patch

from crsbench.evaluation.replay.engine import ReplayEngine
from crsbench.evaluation.replay.models import (
    SessionReplayResult,
    SourcePovRecord,
)


class FakeInfra:
    def __init__(
        self,
        *,
        build_success: bool = True,
        harnesses: list[str] | None = None,
    ) -> None:
        self.build_success = build_success
        self.harnesses = harnesses or ["fuzz-a"]
        self.build_calls: list[tuple[str, str]] = []

    def build_project_fuzzers(
        self,
        project_name: str,
        *,
        sanitizer: str = "address",
        timeout: int = 3600,
    ):
        del timeout
        self.build_calls.append((project_name, sanitizer))
        return type(
            "BuildResult",
            (),
            {"success": self.build_success, "stdout": "", "stderr": ""},
        )()

    def list_fuzz_targets(self, project_name: str) -> list[str]:
        del project_name
        return list(self.harnesses)

    def classify_reproduce_result(self, *, exit_code: int, stdout: str, stderr: str):
        crashed = exit_code not in (0, 124)
        return type(
            "ReproduceResult",
            (),
            {
                "crashed": crashed,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            },
        )()


class FakeSessionPool:
    def __init__(
        self,
        result: SessionReplayResult | None = None,
        *,
        results_by_task: dict[tuple[str, str], SessionReplayResult] | None = None,
    ) -> None:
        self.result = result
        self.results_by_task = results_by_task or {}
        self.run_calls: list[tuple[str, str]] = []

    def run_many(self, tasks, timeout: int):
        del timeout
        out = {}
        for task in tasks:
            self.run_calls.append((task.target_harness, task.pov_content_hash))
            task_key = (task.target_harness, task.pov_content_hash)
            if task_key in self.results_by_task:
                out[task] = self.results_by_task[task_key]
                continue
            if self.result is not None:
                out[task] = self.result
                continue
            raise AssertionError(
                "FakeSessionPool missing result for task "
                f"harness={task.target_harness} "
                f"pov_content_hash={task.pov_content_hash}"
            )
        return out

    def close(self) -> None:
        return None


def _record(
    source_dir: Path,
    trial_path: str,
    pov_path: Path,
    content_hash: str,
    *,
    benchmark: str = "afc-curl-delta-01",
) -> SourcePovRecord:
    return SourcePovRecord(
        source_id="source-123456789abc",
        source_dir=source_dir,
        experiment_name=source_dir.name,
        trial_relative_path=trial_path,
        benchmark=benchmark,
        source_harness="curl_fuzzer",
        source_sanitizer="address",
        original_pov_path=pov_path,
        original_pov_relpath=str(pov_path.relative_to(source_dir)),
        pov_filename=pov_path.name,
        pov_content_hash=content_hash,
    )


def _session_result(
    *,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    crashed: bool | None,
    duration_seconds: float = 1.0,
    timed_out: bool = False,
    session_restarted: bool = False,
    error_message: str | None = None,
) -> SessionReplayResult:
    return SessionReplayResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration_seconds,
        timed_out=timed_out,
        session_restarted=session_restarted,
        crashed=crashed,
        error_message=error_message,
    )


def _run_0day_replay(tmp_path: Path) -> Path:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    crash_pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    no_crash_pov = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    crash_pov.parent.mkdir(parents=True, exist_ok=True)
    no_crash_pov.parent.mkdir(parents=True, exist_ok=True)
    crash_pov.write_bytes(b"CRASH")
    no_crash_pov.write_bytes(b"NOCRASH")
    crash_hash = "44" * 32
    no_crash_hash = "55" * 32
    records = [
        _record(source_dir, "trial-a", crash_pov, crash_hash),
        _record(source_dir, "trial-b", no_crash_pov, no_crash_hash),
    ]
    pool = FakeSessionPool(
        results_by_task={
            ("fuzz-a", crash_hash): _session_result(
                exit_code=77,
                stdout="stdout crash",
                stderr="stderr crash",
                crashed=True,
            ),
            ("fuzz-b", crash_hash): _session_result(
                exit_code=77,
                stdout="stdout crash",
                stderr="stderr crash",
                crashed=True,
            ),
            ("fuzz-a", no_crash_hash): _session_result(
                exit_code=0,
                stdout="stdout ok",
                stderr="stderr ok",
                crashed=False,
            ),
            ("fuzz-b", no_crash_hash): _session_result(
                exit_code=0,
                stdout="stdout ok",
                stderr="stderr ok",
                crashed=False,
            ),
        }
    )
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(harnesses=["fuzz-a", "fuzz-b"]),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: pool,
    )

    engine.run(records, source_dirs=[source_dir])

    return tmp_path / "replay-out"


def _run_mixed_outcome_0day_replay(tmp_path: Path) -> Path:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"MIXED")
    pov_hash = "66" * 32
    records = [_record(source_dir, "trial-a", pov, pov_hash)]
    pool = FakeSessionPool(
        results_by_task={
            ("fuzz-a", pov_hash): _session_result(
                exit_code=77,
                stdout="stdout crash",
                stderr="stderr crash",
                crashed=True,
            ),
            ("fuzz-b", pov_hash): _session_result(
                exit_code=0,
                stdout="stdout ok",
                stderr="stderr ok",
                crashed=False,
            ),
        }
    )
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(harnesses=["fuzz-a", "fuzz-b"]),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: pool,
    )

    engine.run(records, source_dirs=[source_dir])

    return tmp_path / "replay-out"


def test_replay_engine_deduplicates_physical_execution_but_preserves_provenance(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov_a = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov_b = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    pov_a.parent.mkdir(parents=True, exist_ok=True)
    pov_b.parent.mkdir(parents=True, exist_ok=True)
    pov_a.write_bytes(b"SAME")
    pov_b.write_bytes(b"SAME")

    shared_hash = "00" * 32
    records = [
        _record(source_dir, "trial-a", pov_a, shared_hash),
        _record(source_dir, "trial-b", pov_b, shared_hash),
    ]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr="stderr crash",
            crashed=True,
        )
    )
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: pool,
    )

    engine.run(records, source_dirs=[source_dir])

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert pool.run_calls == [("fuzz-a", shared_hash)]
    assert len(data) == 2
    assert (
        data[0]["replays"][0]["artifact_dir"] == data[1]["replays"][0]["artifact_dir"]
    )
    assert {entry["trial_relative_path"] for entry in data} == {"trial-a", "trial-b"}
    assert {entry["original_pov_relpath"] for entry in data} == {
        "trial-a/output/povs/a.blob",
        "trial-b/output/povs/b.blob",
    }
    assert {entry["pov_filename"] for entry in data} == {"a.blob", "b.blob"}

    sanitizer_log = Path(data[0]["replays"][0]["sanitizer_log"])
    assert sanitizer_log.read_text(encoding="utf-8") == (
        "stdout crash\n===== STDERR =====\nstderr crash"
    )


def test_replay_engine_counts_0day_rows_per_source_record_after_dedup(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov_a = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov_b = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    pov_a.parent.mkdir(parents=True, exist_ok=True)
    pov_b.parent.mkdir(parents=True, exist_ok=True)
    pov_a.write_bytes(b"SAME")
    pov_b.write_bytes(b"SAME")

    shared_hash = "77" * 32
    records = [
        _record(source_dir, "trial-a", pov_a, shared_hash),
        _record(source_dir, "trial-b", pov_b, shared_hash),
    ]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr="stderr crash",
            crashed=True,
        )
    )
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: pool,
    )

    engine.run(records, source_dirs=[source_dir])

    summary = json.loads((tmp_path / "replay-out" / "summary.json").read_text())
    assert summary["physical_replay_tasks"] == 1
    assert summary["crash_count"] == 1
    assert summary["0day_count"] == 2
    assert summary["crashing_replay_count"] == 2

    zero_day = json.loads((tmp_path / "replay-out" / "0day.json").read_text())
    assert len(zero_day) == 2
    assert {entry["trial_relative_path"] for entry in zero_day} == {
        "trial-a",
        "trial-b",
    }


def test_replay_engine_records_and_logs_throughput_metrics(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov_a = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov_b = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    pov_a.parent.mkdir(parents=True, exist_ok=True)
    pov_b.parent.mkdir(parents=True, exist_ok=True)
    pov_a.write_bytes(b"SAME")
    pov_b.write_bytes(b"SAME")
    records = [
        _record(source_dir, "trial-a", pov_a, "33" * 32),
        _record(source_dir, "trial-b", pov_b, "33" * 32),
    ]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr="stderr crash",
            crashed=True,
        )
    )
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: pool,
    )

    with (
        patch("crsbench.evaluation.replay.engine.time.monotonic") as mock_monotonic,
        patch("crsbench.evaluation.replay.engine.logger.info") as mock_info,
    ):
        mock_monotonic.side_effect = [100.0, 104.0]
        engine.run(records, source_dirs=[source_dir])

    summary = json.loads((tmp_path / "replay-out" / "summary.json").read_text())
    assert summary["elapsed_seconds"] == 4.0
    assert summary["naive_replay_tasks"] == 2
    assert summary["physical_replay_tasks"] == 1
    assert summary["deduplicated_replay_tasks_saved"] == 1
    assert summary["physical_replay_tasks_per_second"] == 0.25
    assert summary["original_pov_instances_per_second"] == 0.5
    assert summary["dedup_multiplier"] == 2.0
    mock_info.assert_called_once()
    assert "throughput" in mock_info.call_args.args[0]
    assert "dedup_multiplier=2.000" in mock_info.call_args.args[0]


def test_replay_engine_marks_build_failures_without_aborting_unrelated_work(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"A")
    records = [_record(source_dir, "trial-a", pov, "11" * 32)]

    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(build_success=False),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                crashed=False,
            )
        ),
    )

    engine.run(records, source_dirs=[source_dir])

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert data[0]["status"] == "build_error"
    assert data[0]["replays"] == []


def test_replay_engine_tracks_0day_entries_separately_from_crashing_replays(
    tmp_path: Path,
) -> None:
    replay_out = _run_0day_replay(tmp_path)
    summary = json.loads((replay_out / "summary.json").read_text())
    assert summary["0day_count"] == 1
    assert summary["crashing_replay_count"] == 2


def test_replay_engine_writes_crash_only_0day_rows_without_stdio_fields(
    tmp_path: Path,
) -> None:
    replay_out = _run_0day_replay(tmp_path)
    zero_day_path = replay_out / "0day.json"
    assert zero_day_path.exists()

    data = json.loads(zero_day_path.read_text())
    assert len(data) == 1
    entry = data[0]
    assert entry["status"] == "replayed"
    assert entry["benchmark"] == "afc-curl-delta-01"
    assert entry["original_pov_relpath"] == "trial-a/output/povs/a.blob"
    assert entry["replays"]
    assert {replay["target_harness"] for replay in entry["replays"]} == {
        "fuzz-a",
        "fuzz-b",
    }
    assert all(replay["outcome"] == "crash" for replay in entry["replays"])
    required_replay_fields = {
        "target_harness",
        "sanitizer",
        "outcome",
        "exit_code",
        "duration_seconds",
        "artifact_dir",
        "sanitizer_log",
        "session_restarted",
        "error_message",
    }
    assert all(required_replay_fields.issubset(replay) for replay in entry["replays"])
    assert all("stdout" not in replay for replay in entry["replays"])
    assert all("stderr" not in replay for replay in entry["replays"])
    assert all(
        entry["original_pov_relpath"] != "trial-b/output/povs/b.blob" for entry in data
    )


def test_replay_engine_keeps_mixed_outcome_source_entries_in_0day_with_only_crashes(
    tmp_path: Path,
) -> None:
    replay_out = _run_mixed_outcome_0day_replay(tmp_path)
    summary = json.loads((replay_out / "summary.json").read_text())
    assert summary["0day_count"] == 1
    assert summary["crashing_replay_count"] == 1

    zero_day = json.loads((replay_out / "0day.json").read_text())
    assert len(zero_day) == 1
    assert zero_day[0]["original_pov_relpath"] == "trial-a/output/povs/a.blob"
    assert len(zero_day[0]["replays"]) == 1
    replay = zero_day[0]["replays"][0]
    assert replay["target_harness"] == "fuzz-a"
    assert replay["sanitizer"] == "address"
    assert replay["outcome"] == "crash"
    assert replay["exit_code"] == 77
    assert replay["duration_seconds"] == 1.0
    assert replay["artifact_dir"] is not None
    assert replay["sanitizer_log"] is not None
    assert replay["session_restarted"] is False
    assert replay["error_message"] is None
    assert "stdout" not in replay
    assert "stderr" not in replay


def test_replay_engine_allows_direct_oss_fuzz_project_names_for_discovery_mode(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "gpac").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"A")
    records = [_record(source_dir, "trial-a", pov, "22" * 32, benchmark="gpac")]
    pool = FakeSessionPool(
        _session_result(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.5,
            crashed=False,
        )
    )
    infra = FakeInfra(harnesses=["gpac_fuzzer"])
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=infra,
        mapping={},
        session_pool_factory=lambda **_kwargs: pool,
    )

    engine.run(records, source_dirs=[source_dir])

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert infra.build_calls == [("gpac", "address")]
    assert data[0]["mapped_oss_fuzz_project"] == "gpac"
    assert data[0]["status"] == "replayed"
    assert data[0]["replays"][0]["outcome"] == "no_crash"
