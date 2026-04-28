import subprocess
from pathlib import Path
from unittest.mock import patch

from crsbench.evaluation.replay.session import WarmReplaySession


def test_warm_replay_session_reuses_one_container_for_many_execs(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "projects" / "curl"
    build_out = tmp_path / "build" / "out" / "curl"
    project_dir.mkdir(parents=True)
    build_out.mkdir(parents=True)
    (project_dir / "project.yaml").write_text("language: c\n", encoding="utf-8")
    seed_a = tmp_path / "a.blob"
    seed_b = tmp_path / "b.blob"
    seed_a.write_bytes(b"A")
    seed_b.write_bytes(b"B")
    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        seen_cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(cmd, 0, "container-id\n", "")
        if cmd[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(cmd, 77, "asan output", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        session = WarmReplaySession(
            project_name="curl",
            project_dir=project_dir,
            build_output_dir=build_out,
            output_dir=tmp_path / "session-out",
        )
        try:
            first = session.run("curl_fuzzer", seed_a, timeout=5)
            second = session.run("curl_fuzzer", seed_b, timeout=5)
        finally:
            session.close()

    assert first.exit_code == 77
    assert second.exit_code == 77
    assert len([cmd for cmd in seen_cmds if cmd[:2] == ["docker", "run"]]) == 1
    assert len([cmd for cmd in seen_cmds if cmd[:2] == ["docker", "exec"]]) == 2


def test_warm_replay_session_restarts_once_after_timeout(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects" / "curl"
    build_out = tmp_path / "build" / "out" / "curl"
    project_dir.mkdir(parents=True)
    build_out.mkdir(parents=True)
    (project_dir / "project.yaml").write_text("language: c\n", encoding="utf-8")
    seed = tmp_path / "seed.blob"
    seed.write_bytes(b"A")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(["docker", "run"], 0, "container-1\n", ""),
            subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=5),
            subprocess.CompletedProcess(["docker", "rm", "-f"], 0, "", ""),
            subprocess.CompletedProcess(["docker", "run"], 0, "container-2\n", ""),
            subprocess.CompletedProcess(["docker", "exec"], 77, "asan output", ""),
            subprocess.CompletedProcess(["docker", "rm", "-f"], 0, "", ""),
        ]

        session = WarmReplaySession(
            project_name="curl",
            project_dir=project_dir,
            build_output_dir=build_out,
            output_dir=tmp_path / "session-out",
        )
        try:
            result = session.run("curl_fuzzer", seed, timeout=5)
        finally:
            session.close()

    assert result.session_restarted is True
    assert result.exit_code == 77
