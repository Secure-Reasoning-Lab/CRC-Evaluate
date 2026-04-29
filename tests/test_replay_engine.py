import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from crsbench.evaluation.replay.engine import ReplayEngine
from crsbench.evaluation.replay.models import (
    SessionReplayResult,
    SourcePovRecord,
)


def _asan_stderr(crash_type: str = "heap-buffer-overflow") -> str:
    return (
        f"==1==ERROR: AddressSanitizer: {crash_type} on address 0x1\n"
        "#0 0xabc in LLVMFuzzerTestOneInput /src/fuzzer.cc:42:7\n"
        "#1 0xdef in main /src/driver.cc:9:3\n"
        f"SUMMARY: AddressSanitizer: {crash_type} /src/example.c:42:7"
    )


def _asan_oom_stderr() -> str:
    return (
        "==1==ERROR: AddressSanitizer: allocator is out of memory trying to "
        "allocate 0x1000 bytes\n"
        "SUMMARY: AddressSanitizer: out-of-memory"
    )


def _java_exception_stderr(
    exception_type: str = "java.lang.IllegalStateException",
) -> str:
    return (
        f'Exception in thread "main" {exception_type}: boom\n'
        "\tat com.example.Fuzzer.fuzzerTestOneInput(Fuzzer.java:42)\n"
        "\tat com.example.Runner.main(Runner.java:10)\n"
    )


def _jazzer_java_exception_stderr(
    exception_type: str = "java.lang.IllegalStateException",
) -> str:
    return (
        f"== Java Exception: {exception_type}\n"
        "\tat com.example.Fuzzer.fuzzerTestOneInput(Fuzzer.java:42)\n"
        "\tat com.example.Runner.main(Runner.java:10)\n"
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

    def run_many(self, tasks, timeout: int, on_result=None):
        del timeout
        out = {}
        for task in tasks:
            self.run_calls.append((task.target_harness, task.pov_content_hash))
            task_key = (task.target_harness, task.pov_content_hash)
            if task_key in self.results_by_task:
                out[task] = self.results_by_task[task_key]
                if on_result is not None:
                    on_result(task, out[task])
                continue
            if self.result is not None:
                out[task] = self.result
                if on_result is not None:
                    on_result(task, out[task])
                continue
            raise AssertionError(
                "FakeSessionPool missing result for task "
                f"harness={task.target_harness} "
                f"pov_content_hash={task.pov_content_hash}"
            )
        return out

    def close(self) -> None:
        return None


class ConcurrentBuildInfra(FakeInfra):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._both_builds_started = threading.Event()
        self._builds_started = 0
        self.active_builds = 0
        self.max_active_builds = 0

    def build_project_fuzzers(
        self,
        project_name: str,
        *,
        sanitizer: str = "address",
        timeout: int = 3600,
    ):
        del timeout
        with self._lock:
            self.build_calls.append((project_name, sanitizer))
            self._builds_started += 1
            self.active_builds += 1
            self.max_active_builds = max(self.max_active_builds, self.active_builds)
            if self._builds_started >= 2:
                self._both_builds_started.set()

        self._both_builds_started.wait(timeout=1)
        time.sleep(0.05)

        with self._lock:
            self.active_builds -= 1

        return type(
            "BuildResult",
            (),
            {"success": self.build_success, "stdout": "", "stderr": ""},
        )()


class PartialResultPool:
    def __init__(self, first_result: SessionReplayResult) -> None:
        self.first_result = first_result

    def run_many(self, tasks, timeout: int, on_result=None):
        del timeout
        first_task = tasks[0]
        if on_result is not None:
            on_result(first_task, self.first_result)
        raise RuntimeError("mid-group failure")

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
                stderr=_asan_stderr(),
                crashed=True,
            ),
            ("fuzz-b", crash_hash): _session_result(
                exit_code=77,
                stdout="stdout crash",
                stderr=_asan_stderr("use-after-free"),
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
                stderr=_asan_stderr(),
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
            stderr=_asan_stderr(),
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
        f"stdout crash\n===== STDERR =====\n{_asan_stderr()}"
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
            stderr=_asan_stderr(),
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
    assert summary["0day_raw_count"] == 2
    assert summary["0day_dedup_count"] == 1
    assert summary["crashing_replay_count"] == 2

    zero_day = json.loads((tmp_path / "replay-out" / "0day.json").read_text())
    assert len(zero_day) == 2
    rows_by_trial = {
        entry["trial_relative_path"]: {
            "original_pov_relpath": entry["original_pov_relpath"],
            "pov_filename": entry["pov_filename"],
        }
        for entry in zero_day
    }
    assert rows_by_trial == {
        "trial-a": {
            "original_pov_relpath": "trial-a/output/povs/a.blob",
            "pov_filename": "a.blob",
        },
        "trial-b": {
            "original_pov_relpath": "trial-b/output/povs/b.blob",
            "pov_filename": "b.blob",
        },
    }

    deduped = json.loads((tmp_path / "replay-out" / "0day-dedup.json").read_text())
    assert len(deduped) == 1
    assert deduped[0]["benchmark"] == "afc-curl-delta-01"
    assert deduped[0]["mapped_oss_fuzz_project"] == "curl"
    assert deduped[0]["sanitizer"] == "address"
    assert deduped[0]["crash_type"] == "heap-buffer-overflow"
    assert deduped[0]["signature_source"] == "parsed"
    assert deduped[0]["source_entry_count"] == 2
    assert deduped[0]["replay_count"] == 2
    assert {entry["trial_relative_path"] for entry in deduped[0]["entries"]} == {
        "trial-a",
        "trial-b",
    }
    assert all(len(entry["replays"]) == 1 for entry in deduped[0]["entries"])


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
        patch.object(engine, "_monotonic") as mock_monotonic,
        patch("crsbench.evaluation.replay.engine.logger.info") as mock_info,
    ):
        mock_monotonic.side_effect = [
            100.0,  # run start
            100.2,  # planning end / group execution start
            100.3,  # group start
            100.3,  # lock wait start
            100.4,  # lock acquired
            100.4,  # build+prepare start
            101.2,  # build+prepare end
            101.2,  # session pool setup start
            101.5,  # session pool setup end
            101.5,  # replay start
            103.0,  # replay end
            103.0,  # result aggregation start
            103.6,  # result aggregation end
            103.7,  # group execution end / finalization start
            104.0,  # finalization end
        ]
        engine.run(records, source_dirs=[source_dir])

    summary = json.loads((tmp_path / "replay-out" / "summary.json").read_text())
    group_summary = json.loads(
        (tmp_path / "replay-out" / "group-summary.json").read_text()
    )
    assert summary["elapsed_seconds"] == 4.0
    assert summary["naive_replay_tasks"] == 2
    assert summary["physical_replay_tasks"] == 1
    assert summary["deduplicated_replay_tasks_saved"] == 1
    assert summary["physical_replay_tasks_per_second"] == 0.25
    assert summary["original_pov_instances_per_second"] == 0.5
    assert summary["dedup_multiplier"] == 2.0
    assert summary["original_pov_instances_total"] == 2
    assert summary["current_run"] == {
        "group_count_total": 1,
        "group_count_executed": 1,
        "group_count_reused": 0,
        "physical_replay_tasks_executed": 1,
        "physical_replay_tasks_reused": 0,
        "timing": {
            "planning_wall_seconds": 0.2,
            "group_execution_wall_seconds": 3.5,
            "finalization_wall_seconds": 0.3,
            "lock_wait_active_wall_seconds": 0.1,
            "build_and_prepare_active_wall_seconds": 0.8,
            "session_pool_setup_active_wall_seconds": 0.3,
            "replay_active_wall_seconds": 1.5,
            "result_aggregation_active_wall_seconds": 0.6,
            "lock_wait_wall_seconds_sum": 0.1,
            "build_and_prepare_wall_seconds_sum": 0.8,
            "session_pool_setup_wall_seconds_sum": 0.3,
            "replay_wall_seconds_sum": 1.5,
            "result_aggregation_wall_seconds_sum": 0.6,
            "task_duration_seconds_sum": 1.0,
            "task_duration_seconds_max": 1.0,
        },
    }
    assert group_summary == [
        {
            "mapped_project": "curl",
            "sanitizer": "address",
            "source_pov_instances": 2,
            "checkpoint_reused": False,
            "naive_replay_tasks": 2,
            "physical_replay_tasks": 1,
            "summary_updates": {
                "projects_built": 1,
                "unique_replay_tasks_executed": 1,
                "crash_count": 1,
            },
            "timing": {
                "group_wall_seconds": 3.3,
                "lock_wait_wall_seconds": 0.1,
                "build_and_prepare_wall_seconds": 0.8,
                "session_pool_setup_wall_seconds": 0.3,
                "replay_wall_seconds": 1.5,
                "result_aggregation_wall_seconds": 0.6,
                "task_duration_seconds_sum": 1.0,
                "task_duration_seconds_max": 1.0,
                "task_duration_seconds_avg": 1.0,
                "session_count": 1,
            },
            "phase_intervals": {
                "project_lock_wait": {
                    "started_offset_seconds": 0.3,
                    "ended_offset_seconds": 0.4,
                    "wall_seconds": 0.1,
                },
                "build_and_prepare": {
                    "started_offset_seconds": 0.4,
                    "ended_offset_seconds": 1.2,
                    "wall_seconds": 0.8,
                },
                "session_pool_setup": {
                    "started_offset_seconds": 1.2,
                    "ended_offset_seconds": 1.5,
                    "wall_seconds": 0.3,
                },
                "replay": {
                    "started_offset_seconds": 1.5,
                    "ended_offset_seconds": 3.0,
                    "wall_seconds": 1.5,
                },
                "result_aggregation": {
                    "started_offset_seconds": 3.0,
                    "ended_offset_seconds": 3.6,
                    "wall_seconds": 0.6,
                },
            },
        }
    ]
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


def test_replay_engine_can_overlap_independent_project_groups(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    (latest_projects / "zlib").mkdir(parents=True)
    pov_a = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov_b = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    pov_a.parent.mkdir(parents=True, exist_ok=True)
    pov_b.parent.mkdir(parents=True, exist_ok=True)
    pov_a.write_bytes(b"A")
    pov_b.write_bytes(b"B")
    records = [
        _record(source_dir, "trial-a", pov_a, "aa" * 32, benchmark="bench-a"),
        _record(source_dir, "trial-b", pov_b, "bb" * 32, benchmark="bench-b"),
    ]
    infra = ConcurrentBuildInfra()
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        group_jobs=2,
        per_pov_timeout=5,
        infra=infra,
        mapping={"bench-a": "curl", "bench-b": "zlib"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                crashed=False,
            )
        ),
    )

    engine.run(records, source_dirs=[source_dir])

    summary = json.loads((tmp_path / "replay-out" / "summary.json").read_text())
    assert summary["projects_built"] == 2
    assert infra.max_active_builds == 2
    assert sorted(infra.build_calls) == [("curl", "address"), ("zlib", "address")]


def test_replay_engine_resume_skips_completed_groups(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    (latest_projects / "zlib").mkdir(parents=True)
    pov_a = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov_b = source_dir / "trial-b" / "output" / "povs" / "b.blob"
    pov_a.parent.mkdir(parents=True, exist_ok=True)
    pov_b.parent.mkdir(parents=True, exist_ok=True)
    pov_a.write_bytes(b"A")
    pov_b.write_bytes(b"B")
    record_a = _record(source_dir, "trial-a", pov_a, "aa" * 32, benchmark="bench-a")
    record_b = _record(source_dir, "trial-b", pov_b, "bb" * 32, benchmark="bench-b")

    first_engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        group_jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(),
        mapping={"bench-a": "curl", "bench-b": "zlib"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                crashed=False,
            )
        ),
    )
    first_engine.run([record_a], source_dirs=[source_dir])

    resume_infra = FakeInfra()
    resume_engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        group_jobs=1,
        per_pov_timeout=5,
        infra=resume_infra,
        resume=True,
        mapping={"bench-a": "curl", "bench-b": "zlib"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                crashed=False,
            )
        ),
    )
    resume_engine.run([record_a, record_b], source_dirs=[source_dir])

    assert resume_infra.build_calls == [("zlib", "address")]
    summary = json.loads((tmp_path / "replay-out" / "summary.json").read_text())
    group_summary = json.loads(
        (tmp_path / "replay-out" / "group-summary.json").read_text()
    )
    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert summary["original_pov_instances_total"] == 2
    assert summary["current_run"]["group_count_total"] == 2
    assert summary["current_run"]["group_count_executed"] == 1
    assert summary["current_run"]["group_count_reused"] == 1
    assert summary["current_run"]["physical_replay_tasks_executed"] == 1
    assert summary["current_run"]["physical_replay_tasks_reused"] == 1
    assert {
        (entry["mapped_project"], entry["checkpoint_reused"]) for entry in group_summary
    } == {("curl", True), ("zlib", False)}
    assert any(
        entry["mapped_project"] == "curl" and entry["phase_intervals"] == {}
        for entry in group_summary
    )
    assert {entry["mapped_oss_fuzz_project"] for entry in data} == {"curl", "zlib"}


def test_replay_engine_writes_incremental_0day_log_before_group_completes(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"CRASH")
    record = _record(source_dir, "trial-a", pov, "cc" * 32)
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        group_jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(harnesses=["fuzz-a", "fuzz-b"]),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: PartialResultPool(
            _session_result(
                exit_code=77,
                stdout="stdout crash",
                stderr=_asan_stderr(),
                crashed=True,
            )
        ),
    )

    engine.run([record], source_dirs=[source_dir])

    zero_day_log = tmp_path / "replay-out" / "0day.log"
    assert zero_day_log.exists()
    lines = [json.loads(line) for line in zero_day_log.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["status"] == "replayed"
    assert lines[0]["replays"][0]["target_harness"] == "fuzz-a"

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert data[0]["status"] == "error"


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


def test_replay_engine_writes_deduplicated_0day_groups_by_crash_signature(
    tmp_path: Path,
) -> None:
    replay_out = _run_0day_replay(tmp_path)
    dedup_path = replay_out / "0day-dedup.json"
    assert dedup_path.exists()

    data = json.loads(dedup_path.read_text())
    assert len(data) == 2
    assert {(entry["crash_type"], entry["signature_source"]) for entry in data} == {
        ("heap-buffer-overflow", "parsed"),
        ("use-after-free", "parsed"),
    }
    assert {entry["source_entry_count"] for entry in data} == {1}
    assert {entry["replay_count"] for entry in data} == {1}
    assert {entry["mapped_oss_fuzz_project"] for entry in data} == {"curl"}
    assert {entry["benchmark"] for entry in data} == {"afc-curl-delta-01"}
    assert all(len(entry["entries"]) == 1 for entry in data)
    assert all(len(entry["entries"][0]["replays"]) == 1 for entry in data)
    assert all(
        "stdout" not in replay and "stderr" not in replay
        for entry in data
        for replay in entry["entries"][0]["replays"]
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
    assert replay["outcome"] == "crash"
    assert replay["artifact_dir"] is not None
    assert replay["sanitizer_log"] is not None
    assert "stdout" not in replay
    assert "stderr" not in replay


def test_replay_engine_excludes_unrecognized_crashes_from_0day_outputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"CRASH")
    pov_hash = "88" * 32
    records = [_record(source_dir, "trial-a", pov, pov_hash)]
    pool = FakeSessionPool(
        _session_result(
            exit_code=126,
            stdout="stdout crash",
            stderr="bash: /out/raw.jar: cannot execute binary file: Exec format error",
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
    assert summary["crash_count"] == 1
    assert summary["0day_count"] == 0
    assert summary["crashing_replay_count"] == 0
    assert json.loads((tmp_path / "replay-out" / "0day.json").read_text()) == []
    zero_day_log = tmp_path / "replay-out" / "0day.log"
    assert not zero_day_log.exists() or zero_day_log.read_text() == ""

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert data[0]["replays"][0]["outcome"] == "crash"
    assert data[0]["replays"][0]["artifact_dir"] is not None


def test_replay_engine_includes_java_exception_crashes_in_0day_outputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"CRASH")
    pov_hash = "aa" * 32
    records = [_record(source_dir, "trial-a", pov, pov_hash)]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr=_java_exception_stderr(),
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
    assert summary["crash_count"] == 1
    assert summary["0day_count"] == 1
    assert summary["crashing_replay_count"] == 1
    zero_day = json.loads((tmp_path / "replay-out" / "0day.json").read_text())
    assert len(zero_day) == 1
    assert zero_day[0]["replays"][0]["sanitizer_log"] is not None


def test_replay_engine_includes_jazzer_java_exception_crashes_in_0day_outputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"CRASH")
    pov_hash = "ab" * 32
    records = [_record(source_dir, "trial-a", pov, pov_hash)]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr=_jazzer_java_exception_stderr(),
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
    assert summary["crash_count"] == 1
    assert summary["0day_count"] == 1
    assert summary["crashing_replay_count"] == 1
    zero_day = json.loads((tmp_path / "replay-out" / "0day.json").read_text())
    assert len(zero_day) == 1
    assert zero_day[0]["replays"][0]["sanitizer_log"] is not None


def test_replay_engine_excludes_asan_oom_crashes_from_0day_outputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"CRASH")
    pov_hash = "99" * 32
    records = [_record(source_dir, "trial-a", pov, pov_hash)]
    pool = FakeSessionPool(
        _session_result(
            exit_code=77,
            stdout="stdout crash",
            stderr=_asan_oom_stderr(),
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
    assert summary["crash_count"] == 1
    assert summary["0day_count"] == 0
    assert summary["crashing_replay_count"] == 0
    assert json.loads((tmp_path / "replay-out" / "0day.json").read_text()) == []


def test_replay_engine_resume_ignores_legacy_group_checkpoints(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    latest_projects = tmp_path / "latest-projects"
    (latest_projects / "curl").mkdir(parents=True)
    pov = source_dir / "trial-a" / "output" / "povs" / "a.blob"
    pov.parent.mkdir(parents=True, exist_ok=True)
    pov.write_bytes(b"A")
    record = _record(source_dir, "trial-a", pov, "ab" * 32)

    checkpoint_path = (
        tmp_path
        / "replay-out"
        / ".state"
        / "groups"
        / "curl"
        / "address"
        / "group-result.json"
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_signature = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=FakeInfra(),
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                crashed=False,
            )
        ),
    )._group_input_signature([record])
    checkpoint_path.write_text(
        json.dumps(
            {
                "input_signature": legacy_signature,
                "outcome": {
                    "entries": [],
                    "zero_day_entries": [],
                    "trial_entries": [],
                    "summary_updates": {},
                    "naive_replay_tasks": 0,
                    "physical_replay_tasks": 0,
                },
            },
            indent=2,
        )
    )

    resume_infra = FakeInfra()
    engine = ReplayEngine(
        oss_fuzz_path=tmp_path / "oss-fuzz",
        projects_root=latest_projects,
        output_dir=tmp_path / "replay-out",
        jobs=1,
        per_pov_timeout=5,
        infra=resume_infra,
        resume=True,
        mapping={"afc-curl-delta-01": "curl"},
        session_pool_factory=lambda **_kwargs: FakeSessionPool(
            _session_result(
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                crashed=False,
            )
        ),
    )

    engine.run([record], source_dirs=[source_dir])

    assert resume_infra.build_calls == [("curl", "address")]


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
