"""Subprocess coverage for the notification rehearsal launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_SOURCE = Path("scripts/cloud-rehearsal/test-notification-rehearsal.sh")


def _copy_script_layout(tmp_path: Path) -> tuple[Path, Path]:
    script_dir = tmp_path / "scripts" / "cloud-rehearsal"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "test-notification-rehearsal.sh"
    shutil.copy2(SCRIPT_SOURCE, script_path)
    script_path.chmod(0o755)
    (script_dir / "local-experiment-notification.yaml").write_text(
        "experiment:\n  name: copied-test-config\n",
        encoding="utf-8",
    )
    return script_dir, script_path


def _write_fake_wrapper(script_dir: Path) -> None:
    wrapper_path = script_dir / "run-local-rehearsal.sh"
    wrapper_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

log_file="${FAKE_WRAPPER_LOG:?}"
printf '%s\\n' "$*" >> "${log_file}"
printf 'config=%s\\n' "${CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG:-}" >> "${log_file}"

mode="${FAKE_WRAPPER_MODE:-success}"
state_dir="${CRSBENCH_LOCAL_REHEARSAL_STATE_DIR:?}"

case "${1:-}" in
  up)
    if [[ "${mode}" == "fail" ]]; then
      exit 23
    fi

    mkdir -p "${state_dir}/metadata/orchestrator/attributes"
    mkdir -p "${state_dir}/state/orchestrator"

    if [[ "${mode}" == "success" ]]; then
      python - "${state_dir}/metadata/orchestrator/attributes/crsbench-env-passthrough-b64" <<'PY'
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {
    "CRSBENCH_NOTIFY_APPRISE_URLS": os.environ.get("CRSBENCH_NOTIFY_APPRISE_URLS", ""),
    "OTHER_VALUE": "preserved",
}
path.write_text(
    base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8"),
    encoding="utf-8",
)
PY
    fi
    ;;
  down)
    ;;
esac
""",
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)


def _write_fake_docker(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker_path = bin_dir / "docker"
    docker_path.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

log_file = Path(os.environ["FAKE_DOCKER_LOG"])
with log_file.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
""",
        encoding="utf-8",
    )
    docker_path.chmod(0o755)


def _base_env(
    tmp_path: Path,
    *,
    include_notification_urls: bool = True,
    wrapper_mode: str = "success",
) -> dict[str, str]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_WRAPPER_LOG": str(tmp_path / "wrapper.log"),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_WRAPPER_MODE": wrapper_mode,
        "CRSBENCH_LOCAL_REHEARSAL_STATE_DIR": str(tmp_path / "state"),
        "CRSBENCH_NOTIFY_APPRISE_URLS": "",
    }
    if include_notification_urls:
        env["CRSBENCH_NOTIFY_APPRISE_URLS"] = "discord://token/channel"
    return env


def _run_script(
    script_path: Path,
    *,
    env: dict[str, str],
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script_path), *(args or [])],
        cwd=script_path.parent.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _read_docker_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    return [
        json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
    ]


def test_fails_fast_when_notification_urls_missing(tmp_path: Path) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(
        script_path,
        env=_base_env(tmp_path, include_notification_urls=False),
    )

    assert result.returncode == 1
    assert "CRSBENCH_NOTIFY_APPRISE_URLS" in result.stderr
    assert _read_docker_calls(tmp_path / "docker.log") == []
    assert not (tmp_path / "wrapper.log").exists()


@pytest.mark.parametrize(
    ("args", "expects_dry_run"),
    [([], True), (["--send"], False)],
)
def test_smoke_command_controls_dry_run_flag(
    tmp_path: Path, args: list[str], expects_dry_run: bool
) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(script_path, env=_base_env(tmp_path), args=args)

    assert result.returncode == 0, result.stderr
    docker_calls = _read_docker_calls(tmp_path / "docker.log")
    smoke_calls = [
        call
        for call in docker_calls
        if call[:4] == ["compose", "-f", call[2], "exec"] and "bash" in call
    ]
    assert smoke_calls, docker_calls
    smoke_command = smoke_calls[-1][-1]
    assert ("--dry-run" in smoke_command) is expects_dry_run


def test_keep_up_skips_teardown(tmp_path: Path) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(script_path, env=_base_env(tmp_path), args=["--keep-up"])

    assert result.returncode == 0, result.stderr
    wrapper_calls = (tmp_path / "wrapper.log").read_text(encoding="utf-8").splitlines()
    assert wrapper_calls == [
        "up -d",
        f"config={script_dir / 'local-experiment-notification.yaml'}",
    ]
    assert not any(
        call[:4] == ["compose", "-f", call[2], "down"]
        for call in _read_docker_calls(tmp_path / "docker.log")
    )


def test_wrapper_uses_config_adjacent_to_script(tmp_path: Path) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(script_path, env=_base_env(tmp_path))

    assert result.returncode == 0, result.stderr
    wrapper_calls = (tmp_path / "wrapper.log").read_text(encoding="utf-8").splitlines()
    assert (
        f"config={script_dir / 'local-experiment-notification.yaml'}" in wrapper_calls
    )


def test_missing_metadata_fails_before_smoke_exec(tmp_path: Path) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(
        script_path,
        env=_base_env(tmp_path, wrapper_mode="missing_metadata"),
    )

    assert result.returncode == 1
    assert "expected rendered orchestrator metadata" in result.stderr
    docker_calls = _read_docker_calls(tmp_path / "docker.log")
    assert any(
        call[3:7] == ["exec", "-T", "orchestrator", "true"] for call in docker_calls
    )
    assert not any(
        call[3:7] == ["exec", "-T", "orchestrator", "bash"] for call in docker_calls
    )


def test_wrapper_failure_stops_before_smoke_exec(tmp_path: Path) -> None:
    script_dir, script_path = _copy_script_layout(tmp_path)
    _write_fake_wrapper(script_dir)
    _write_fake_docker(tmp_path / "bin")

    result = _run_script(script_path, env=_base_env(tmp_path, wrapper_mode="fail"))

    assert result.returncode == 23
    assert (tmp_path / "wrapper.log").read_text(encoding="utf-8").splitlines() == [
        "up -d",
        f"config={script_dir / 'local-experiment-notification.yaml'}",
        "down -v",
        f"config={script_dir / 'local-experiment-notification.yaml'}",
    ]
    assert _read_docker_calls(tmp_path / "docker.log") == []
