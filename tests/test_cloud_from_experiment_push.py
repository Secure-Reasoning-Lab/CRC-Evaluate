"""Tests for the ArtifactPusher rsync command and SSH readiness wait."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from crsbench.cloud.collection import (
    ArtifactPusher,
    ArtifactPushError,
    build_push_rsync_cmd,
    wait_for_ssh_ready,
)
from crsbench.cloud.ssh_broker import SshBroker

if TYPE_CHECKING:
    from pathlib import Path


def _fake_instance(name: str = "gce-vm-001", zone: str = "us-central1-a"):
    instance = MagicMock(
        spec=[
            "name",
            "instance_id",
            "status",
            "zone",
            "internal_ip",
            "external_ip",
            "labels",
        ]
    )
    instance.name = name
    instance.instance_id = "1"
    instance.status = "RUNNING"
    instance.zone = zone
    instance.internal_ip = "10.0.0.10"
    instance.external_ip = None
    instance.labels = {}
    return instance


def _fake_fleet(*, ssh_via_iap: bool = False):
    fleet = MagicMock()
    fleet.project = "test-project"
    fleet.zone = "us-central1-a"
    fleet.ssh_via_iap = ssh_via_iap
    return fleet


def test_build_push_rsync_cmd_direct_ip_shape(tmp_path: Path) -> None:
    local = tmp_path / "src"
    local.mkdir()
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("bench/fuzz/pov_store.json\n")

    cmd = build_push_rsync_cmd(
        local_source=local,
        manifest_file=manifest,
        remote_host="user@10.0.0.10",
        remote_destination="/var/lib/crsbench/from-experiment/exp-1",
        ssh_command="ssh -o StrictHostKeyChecking=no",
    )

    assert cmd[0] == "rsync"
    assert "-a" in cmd
    assert "--mkpath" in cmd
    assert f"--files-from={manifest}" in cmd
    assert "--rsync-path=sudo rsync" in cmd
    assert any(part.startswith("--chown=") for part in cmd)
    assert "--delete-delay" not in cmd
    # Source is *local dir with trailing slash*, destination is remote path.
    assert cmd[-2].endswith("src/")
    assert cmd[-1] == "user@10.0.0.10:/var/lib/crsbench/from-experiment/exp-1/"


def test_push_rejects_empty_manifest(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    pusher = ArtifactPusher(broker=broker)

    with pytest.raises(ArtifactPushError, match="manifest is empty"):
        pusher.push_from_experiment(
            instances=[_fake_instance()],
            fleet_by_instance={"gce-vm-001": _fake_fleet()},
            local_source=tmp_path,
            manifest=[],
            remote_destination="/var/lib/crsbench/from-experiment/exp-1",
            experiment_filestore=tmp_path,
        )


def test_push_rejects_missing_local_source(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    pusher = ArtifactPusher(broker=broker)
    missing = tmp_path / "missing-root"

    with pytest.raises(ArtifactPushError, match="not a directory"):
        pusher.push_from_experiment(
            instances=[_fake_instance()],
            fleet_by_instance={"gce-vm-001": _fake_fleet()},
            local_source=missing,
            manifest=["pov_store.json"],
            remote_destination="/var/lib/crsbench/from-experiment/exp-1",
            experiment_filestore=tmp_path,
        )


def test_push_direct_ip_runs_install_and_rsync(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    pusher = ArtifactPusher(broker=broker)
    instance = _fake_instance()
    fleet = _fake_fleet(ssh_via_iap=False)

    with (
        patch.object(
            broker,
            "prepare_direct_known_hosts",
            return_value=tmp_path / "known_hosts",
        ),
        patch.object(broker, "direct_ssh_user", return_value="testuser"),
        patch.object(
            broker,
            "build_direct_ssh_command",
            return_value="ssh -o StrictHostKeyChecking=no",
        ),
        patch.object(
            broker,
            "run_remote_command",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ) as remote_cmd,
        patch("crsbench.cloud.collection.run_rsync_with_retry") as rsync_mock,
    ):
        pusher.push_from_experiment(
            instances=[instance],
            fleet_by_instance={instance.name: fleet},
            local_source=tmp_path,
            manifest=["bench/fuzz/pov_store.json"],
            remote_destination="/var/lib/crsbench/from-experiment/exp-1",
            experiment_filestore=tmp_path,
        )

        # The sudo install -d step ran once with crsbench ownership.
        assert remote_cmd.call_count == 1
        install_cmd = remote_cmd.call_args.kwargs["command"]
        assert "install -d" in install_cmd
        assert "crsbench" in install_cmd

        # rsync ran with --files-from and crsbench chown.
        assert rsync_mock.call_count == 1
        rsync_args = rsync_mock.call_args.args[0]
        assert any(a.startswith("--files-from=") for a in rsync_args)
        assert "--chown=crsbench:crsbench" in rsync_args


def test_push_reports_per_instance_failures(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    pusher = ArtifactPusher(broker=broker)
    ok_instance = _fake_instance(name="gce-vm-ok")
    bad_instance = _fake_instance(name="gce-vm-bad")
    fleet = _fake_fleet(ssh_via_iap=False)

    def _push_one(*, instance, **_):
        if instance.name == "gce-vm-bad":
            raise RuntimeError("boom")

    with patch.object(pusher, "_push_one", side_effect=_push_one):
        with pytest.raises(ArtifactPushError, match="gce-vm-bad"):
            pusher.push_from_experiment(
                instances=[ok_instance, bad_instance],
                fleet_by_instance={
                    ok_instance.name: fleet,
                    bad_instance.name: fleet,
                },
                local_source=tmp_path,
                manifest=["bench/fuzz/pov_store.json"],
                remote_destination="/var/lib/crsbench/from-experiment/exp-1",
                experiment_filestore=tmp_path,
            )


def test_wait_for_ssh_ready_succeeds_when_command_returns_zero(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    instance = _fake_instance()
    fleet = _fake_fleet()

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch.object(broker, "prepare_direct_known_hosts", return_value=None),
        patch.object(broker, "direct_ssh_user", return_value="u"),
        patch.object(broker, "run_remote_command", return_value=ok),
    ):
        wait_for_ssh_ready(
            [instance],
            broker=broker,
            fleet_by_instance={instance.name: fleet},
            experiment_filestore=tmp_path,
            timeout_sec=5.0,
            poll_interval_sec=0.01,
        )


def test_wait_for_ssh_ready_times_out(tmp_path: Path) -> None:
    broker = SshBroker(base_path=tmp_path)
    instance = _fake_instance()
    fleet = _fake_fleet()

    fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with (
        patch.object(broker, "prepare_direct_known_hosts", return_value=None),
        patch.object(broker, "direct_ssh_user", return_value="u"),
        patch.object(broker, "run_remote_command", return_value=fail),
    ):
        with pytest.raises(ArtifactPushError, match="Timed out"):
            wait_for_ssh_ready(
                [instance],
                broker=broker,
                fleet_by_instance={instance.name: fleet},
                experiment_filestore=tmp_path,
                timeout_sec=0.2,
                poll_interval_sec=0.05,
            )
