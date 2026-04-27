import json
from pathlib import Path

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
    def __init__(self, result: SessionReplayResult) -> None:
        self.result = result
        self.run_calls: list[tuple[str, str]] = []

    def run_many(self, tasks, timeout: int):
        del timeout
        out = {}
        for task in tasks:
            self.run_calls.append((task.target_harness, task.pov_content_hash))
            out[task] = self.result
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
        SessionReplayResult(
            exit_code=77,
            stdout="stdout crash",
            stderr="stderr crash",
            duration_seconds=1.0,
            timed_out=False,
            session_restarted=False,
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

    sanitizer_log = Path(data[0]["replays"][0]["sanitizer_log"])
    assert sanitizer_log.read_text(encoding="utf-8") == (
        "stdout crash\n===== STDERR =====\nstderr crash"
    )


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
            SessionReplayResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
                session_restarted=False,
                crashed=False,
            )
        ),
    )

    engine.run(records, source_dirs=[source_dir])

    data = json.loads((tmp_path / "replay-out" / "pov-to-crash-map.json").read_text())
    assert data[0]["status"] == "build_error"
    assert data[0]["replays"] == []


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
        SessionReplayResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.5,
            timed_out=False,
            session_restarted=False,
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
