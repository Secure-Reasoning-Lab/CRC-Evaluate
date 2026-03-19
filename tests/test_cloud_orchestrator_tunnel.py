"""Tests for the remote orchestrator Redis tunnel helper."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.cloud.launch_state import CloudLaunchState
from crsbench.cloud.orchestrator_tunnel import (
    OrchestratorRedisTunnel,
    OrchestratorTunnelError,
    build_tunnel_command,
    wait_for_local_port,
)
from crsbench.cloud.types import CloudProvider


def _make_launch_state(
    *,
    ssh_via_iap: bool,
    external_ip: str | None = "34.1.2.50",
    internal_ip: str | None = "10.0.0.50",
) -> CloudLaunchState:
    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        orchestrator_internal_ip=internal_ip,
        orchestrator_external_ip=external_ip,
        orchestrator_ssh_via_iap=ssh_via_iap,
    )


class _DummyProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.wait_calls: list[float | None] = []

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0

    def kill(self) -> None:
        self.terminated = True


def test_build_tunnel_command_for_direct_ssh():
    launch_state = _make_launch_state(ssh_via_iap=False)

    with (
        patch(
            "crsbench.cloud.orchestrator_tunnel.prepare_known_hosts",
            return_value=Path("/tmp/known_hosts"),
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.resolve_direct_ssh_user",
            return_value="alice",
        ),
    ):
        cmd = build_tunnel_command(
            Path("/tmp/config.yaml"),
            launch_state,
            local_port=16379,
        )

    assert cmd[0] == "ssh"
    assert "-N" in cmd
    assert "-L" in cmd
    assert "16379:127.0.0.1:6379" in cmd
    assert "alice@34.1.2.50" in cmd


def test_build_tunnel_command_for_iap():
    launch_state = _make_launch_state(ssh_via_iap=True)

    cmd = build_tunnel_command(
        Path("/tmp/config.yaml"),
        launch_state,
        local_port=16379,
    )

    assert cmd[:4] == ["gcloud", "compute", "ssh", "gce-orchestrator-test-exp"]
    assert "--project=test-project" in cmd
    assert "--zone=us-east5-b" in cmd
    assert "--tunnel-through-iap" in cmd
    assert "--" in cmd
    assert "-N" in cmd
    assert "-L" in cmd
    assert "16379:127.0.0.1:6379" in cmd


def test_tunnel_waits_for_local_port_before_returning():
    process = _DummyProcess()
    launch_state = _make_launch_state(ssh_via_iap=True)

    with (
        patch(
            "crsbench.cloud.orchestrator_tunnel.build_tunnel_command",
            return_value=["ssh", "-N", "-L", "16379:127.0.0.1:6379"],
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.shutil.which", return_value="/bin/ssh"
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.subprocess.Popen",
            return_value=process,
        ),
        patch("crsbench.cloud.orchestrator_tunnel.wait_for_local_port") as mock_wait,
    ):
        with OrchestratorRedisTunnel.from_launch_state(
            Path("/tmp/config.yaml"),
            launch_state,
            local_port=16379,
        ) as tunnel:
            assert tunnel.redis_host == "127.0.0.1:16379"

    mock_wait.assert_called_once_with(
        "127.0.0.1",
        16379,
        timeout=5.0,
        process=process,
        process_label="orchestrator tunnel process",
    )
    assert process.terminated is True


def test_tunnel_cleans_up_on_exception_exit():
    process = _DummyProcess()
    launch_state = _make_launch_state(ssh_via_iap=True)

    with (
        patch(
            "crsbench.cloud.orchestrator_tunnel.build_tunnel_command",
            return_value=["ssh", "-N", "-L", "16379:127.0.0.1:6379"],
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.shutil.which", return_value="/bin/ssh"
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.subprocess.Popen",
            return_value=process,
        ),
        patch("crsbench.cloud.orchestrator_tunnel.wait_for_local_port"),
        patch.object(process, "wait", return_value=0),
    ):
        try:
            with OrchestratorRedisTunnel.from_launch_state(
                Path("/tmp/config.yaml"),
                launch_state,
                local_port=16379,
            ):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    assert process.terminated is True


def test_tunnel_retries_transient_startup_failures():
    first_process = _DummyProcess()
    second_process = _DummyProcess()
    launch_state = _make_launch_state(ssh_via_iap=True)

    with (
        patch(
            "crsbench.cloud.orchestrator_tunnel.build_tunnel_command",
            return_value=["ssh", "-N", "-L", "16379:127.0.0.1:6379"],
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.shutil.which", return_value="/bin/ssh"
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.subprocess.Popen",
            side_effect=[first_process, second_process],
        ),
        patch(
            "crsbench.cloud.orchestrator_tunnel.wait_for_local_port",
            side_effect=[OrchestratorTunnelError("not ready"), None],
        ) as mock_wait,
        patch("crsbench.cloud.orchestrator_tunnel.time.sleep") as mock_sleep,
    ):
        with OrchestratorRedisTunnel.from_launch_state(
            Path("/tmp/config.yaml"),
            launch_state,
            local_port=16379,
            startup_timeout=30.0,
        ) as tunnel:
            assert tunnel.redis_host == "127.0.0.1:16379"

    assert mock_wait.call_count == 2
    mock_wait.assert_any_call(
        "127.0.0.1",
        16379,
        timeout=5.0,
        process=first_process,
        process_label="orchestrator tunnel process",
    )
    mock_wait.assert_any_call(
        "127.0.0.1",
        16379,
        timeout=5.0,
        process=second_process,
        process_label="orchestrator tunnel process",
    )
    mock_sleep.assert_called_once_with(1.0)
    assert first_process.terminated is True
    assert second_process.terminated is True


def test_wait_for_local_port_fails_fast_when_tunnel_process_exits() -> None:
    process = _DummyProcess()

    with (
        patch(
            "crsbench.cloud.orchestrator_tunnel.socket.create_connection",
            side_effect=socket.error("connection refused"),
        ),
        patch.object(process, "poll", return_value=255),
        patch("crsbench.cloud.orchestrator_tunnel.time.sleep"),
        pytest.raises(
            OrchestratorTunnelError,
            match="orchestrator tunnel process exited with code 255",
        ),
    ):
        wait_for_local_port(
            "127.0.0.1",
            16379,
            timeout=1.0,
            process=process,
            process_label="orchestrator tunnel process",
        )
