"""Tests for ArtifactCollector — ARTF-01 through ARTF-04."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.cloud.collection import (
    ArtifactCollectionError,
    ArtifactCollector,
    collect_marker_path,
    discover_experiment_start_time_from_staging,
    merge_experiment_start_time,
    read_collect_marker,
    write_collect_marker,
)
from crsbench.cloud.gce.models import GceWorkerRecord
from crsbench.reporting.snapshot_loader import discover_trials
from crsbench.validation.schemas import GceWorkerFleetConfig

_REAL_SUBPROCESS_RUN = subprocess.run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    name: str = "gce-worker-001",
    internal_ip: str = "10.0.0.10",
    external_ip: str | None = None,
    zone: str = "us-central1-a",
) -> GceWorkerRecord:
    return GceWorkerRecord(
        name=name,
        instance_id="1001",
        status="RUNNING",
        zone=zone,
        internal_ip=internal_ip,
        external_ip=external_ip,
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        labels={},
        raw={},
    )


def _make_fleet(
    *,
    ssh_via_iap: bool = False,
    zone: str = "us-central1-a",
) -> GceWorkerFleetConfig:
    return GceWorkerFleetConfig(
        project="test-project",
        zone=zone,
        ssh_via_iap=ssh_via_iap,
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        instance_template="projects/test-project/global/instanceTemplates/crsbench-worker",
        owner_label="team-crs",
    )


def _build_trial_tree(
    base: Path,
    experiment_name: str = "exp-42",
    crs: str = "oss-crs",
    benchmark: str = "curl-delta-01",
    harness: str = "fuzz_http",
    trial_n: int = 1,
    *,
    include_metadata: bool = True,
    mtime: float | None = None,
) -> Path:
    """Create a minimal trial directory tree inside *base* and return the trial dir."""
    trial_dir = (
        base
        / experiment_name
        / crs
        / benchmark
        / harness
        / "delta"
        / "address"
        / f"trial-{trial_n}"
    )
    trial_dir.mkdir(parents=True, exist_ok=True)

    if include_metadata:
        meta = trial_dir / "metadata.json"
        meta.write_text(
            json.dumps(
                {
                    "timestamp": "2026-03-13T00:00:00Z",
                    "trial_num": trial_n,
                    "crs": crs,
                    "benchmark": benchmark,
                    "harness": harness,
                    "mode": "bug_finding",
                    "source": {"path": "/src/curl", "commit": "abc123"},
                }
            )
        )

    # snapshot pair
    snap_archive = trial_dir / "snapshot-0001.tar.gz"
    snap_archive.write_bytes(b"fake-tar-content")
    snap_complete = trial_dir / "snapshot-0001.complete"
    snap_complete.write_text("")

    # worker log
    (trial_dir / "worker.log").write_text("log line\n")

    # seed file with controllable mtime
    seeds_dir = trial_dir / "output" / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    seed_file = seeds_dir / "seed-0001"
    seed_file.write_bytes(b"seed-content")

    if mtime is not None:
        import os

        os.utime(seed_file, (mtime, mtime))

    return trial_dir


def _write_trial_metadata(trial_dir: Path, payload: dict[str, object]) -> None:
    """Write `metadata.json` directly with the supplied payload."""
    metadata_path = trial_dir / "metadata.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")


def _run_local_rsync_from_cloud_cmd(
    cmd: list[str], *, source_root: Path, experiment_name: str
) -> subprocess.CompletedProcess[bytes]:
    """Run a cloud collector rsync command against a local fixture tree."""
    source = cmd[-2]
    destination = cmd[-1]

    if ":" in source:
        remote_source = source.split(":", 1)[1]
        rel = Path(
            remote_source.removeprefix(f"/data/experiments/{experiment_name}").lstrip(
                "/"
            )
        )
        local_source = source_root / experiment_name / rel
    else:
        local_source = Path(source.rstrip("/"))

    local_cmd = ["rsync"]
    skip_next = False
    for arg in cmd[1:-2]:
        if skip_next:
            skip_next = False
            continue
        if arg == "-e":
            skip_next = True
            continue
        if arg.startswith("--rsync-path="):
            continue
        local_cmd.append(arg)

    local_source_arg = str(local_source)
    if source.endswith("/"):
        local_source_arg += "/"
    local_cmd.extend([local_source_arg, destination])
    return _REAL_SUBPROCESS_RUN(local_cmd, check=True)


def _run_local_remote_command_from_cloud_cmd(
    command: str, *, source_root: Path, experiment_name: str
) -> subprocess.CompletedProcess[str]:
    """Run one collector remote command against a local fixture tree."""
    remote_root = f"/data/experiments/{experiment_name}"
    local_root = str((source_root / experiment_name).resolve())
    rewritten = command.replace(remote_root, local_root)
    if rewritten.startswith("sudo "):
        rewritten = rewritten.removeprefix("sudo ")
    return _REAL_SUBPROCESS_RUN(
        ["bash", "-lc", rewritten],
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# ARTF-01 / Task tests: rsync command construction
# ---------------------------------------------------------------------------


class TestRsyncCmdIap:
    """test_rsync_cmd_iap — ARTF-01: IAP SSH transport."""

    def test_iap_tunnel_command_uses_start_iap_tunnel(self) -> None:
        """IAP rsync transport should start a local TCP tunnel instead of shelling through -W."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector(base_path=Path("/tmp/config.yaml"))

        cmd = collector._build_iap_tunnel_command(
            worker=worker,
            fleet=fleet,
            local_port=2222,
        )

        assert cmd[:4] == ["gcloud", "compute", "start-iap-tunnel", worker.name]
        assert "22" in cmd
        assert "--project=test-project" in cmd
        assert "--zone=us-central1-a" in cmd
        assert "--local-host-port=127.0.0.1:2222" in cmd

    def test_iap_tunnel_command_delegates_to_provider_transport(self) -> None:
        """IAP transport details should come from the provider adapter, not shared code."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        transport = MagicMock()
        transport.build_iap_tunnel_command.return_value = ["provider-tunnel"]

        collector = ArtifactCollector(transport=transport)

        cmd = collector._build_iap_tunnel_command(
            worker=worker,
            fleet=fleet,
            local_port=2222,
        )

        assert cmd == ["provider-tunnel"]
        transport.build_iap_tunnel_command.assert_called_once_with(
            instance_name="gce-worker-001",
            project="test-project",
            zone="us-central1-a",
            local_port=2222,
            remote_port=22,
        )

    def test_rsync_cmd_iap_uses_local_ssh_transport(self, tmp_path: Path) -> None:
        """IAP rsync should use a local ssh command against the tunneled localhost port."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")

        ssh_cmd = collector._build_iap_ssh_command(
            local_port=2222,
            ssh_user="alice",
            known_hosts_path=tmp_path / "known_hosts_iap",
            host_key_alias=worker.name,
        )
        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
            ssh_command=ssh_cmd,
            remote_host="127.0.0.1",
        )

        assert "-e" in cmd
        e_idx = cmd.index("-e")
        rendered_ssh_cmd = cmd[e_idx + 1]
        assert rendered_ssh_cmd.startswith("ssh ")
        assert "BatchMode=yes" in rendered_ssh_cmd
        assert "StrictHostKeyChecking=no" in rendered_ssh_cmd
        assert "HostKeyAlias=gce-worker-001" in rendered_ssh_cmd
        assert "UserKnownHostsFile=" in rendered_ssh_cmd
        assert "-p 2222" in rendered_ssh_cmd
        assert "-l alice" in rendered_ssh_cmd
        assert "gcloud compute ssh" not in rendered_ssh_cmd
        assert "-W %h:%p" not in rendered_ssh_cmd

        source = cmd[-2]
        assert source == "127.0.0.1:/data/experiments/exp-42/"

        assert "-a" in cmd
        assert "--mkpath" in cmd
        assert "--partial-dir=.rsync-partial" in cmd
        assert "--delay-updates" in cmd
        assert "--delete-delay" in cmd

    def test_prepare_iap_known_hosts_removes_stale_alias(self, tmp_path: Path) -> None:
        """IAP SSH collection should clear any stale stable-name host key before reconnecting."""
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")
        known_hosts_path = tmp_path / ".crsbench-cloud" / "known_hosts_iap"

        calls: list[list[str]] = []

        def _fake_run(cmd, *_args, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=_fake_run):
            result = collector._prepare_iap_known_hosts(
                experiment_filestore=tmp_path / "filestore",
                host_key_alias="gce-worker-001",
            )

        assert result == known_hosts_path
        assert calls == [
            [
                "ssh-keygen",
                "-R",
                "gce-worker-001",
                "-f",
                str(known_hosts_path),
            ]
        ]


class TestRsyncCmdDirectIp:
    """test_rsync_cmd_direct_ip — ARTF-01: direct-IP SSH transport."""

    def test_rsync_cmd_direct_ip(self) -> None:
        """When ssh_via_iap=False the -e arg uses plain ssh with BatchMode and StrictHostKeyChecking."""
        worker = _make_worker(internal_ip="10.0.0.10")
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        assert "-e" in cmd
        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "ssh" in ssh_cmd
        assert "BatchMode=yes" in ssh_cmd
        assert "StrictHostKeyChecking=yes" in ssh_cmd

        # The source host must be the worker's internal IP (not name)
        source = cmd[-2]  # second-to-last is source, last is dest
        assert worker.internal_ip in source

    def test_rsync_cmd_prefers_external_ip_when_available(self) -> None:
        """Direct SSH collection should prefer a public IP when one exists."""
        worker = _make_worker(internal_ip="10.0.0.10", external_ip="34.1.2.3")
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        source = cmd[-2]
        assert "34.1.2.3" in source

    def test_rsync_cmd_uses_config_scoped_known_hosts_when_base_path_is_set(
        self, tmp_path: Path
    ) -> None:
        """Direct SSH collection should pin host trust to a config-adjacent known_hosts file."""
        worker = _make_worker(internal_ip="10.0.0.10", external_ip="34.1.2.3")
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        collector = ArtifactCollector(base_path=config_path)

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        ssh_cmd = cmd[cmd.index("-e") + 1]
        assert "StrictHostKeyChecking=yes" in ssh_cmd
        assert (
            f"UserKnownHostsFile={tmp_path / '.crsbench-cloud' / 'known_hosts'}"
            in ssh_cmd
        )

    def test_discover_copy_link_filelist_uses_sudo_remote_python(
        self, tmp_path: Path
    ) -> None:
        """Symlink rehydration discovery should enumerate remote paths with sudo privileges."""
        worker = _make_worker(internal_ip="10.0.0.10", external_ip="34.1.2.3")
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")

        seen_command: str | None = None

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal seen_command
            del args
            seen_command = str(kwargs["command"])
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "directories": ["output", "output/empty"],
                        "files": ["output/kept/artifact.txt"],
                    }
                ),
                stderr="",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]

        directories, files = collector._discover_copy_link_filelist(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            experiment_filestore=tmp_path,
            known_hosts_path=None,
            ssh_user=None,
            symlink_relpaths=[Path("output")],
        )

        assert seen_command is not None
        assert seen_command.startswith("sudo python3 -c ")
        assert directories == [Path("output"), Path("output/empty")]
        assert files == ["output/kept/artifact.txt"]

    def test_discover_copy_link_filelist_reuses_supplied_ssh_transport(
        self, tmp_path: Path
    ) -> None:
        """Discovery should reuse a caller-provided SSH transport instead of reopening it."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")
        seen_cmds: list[list[str]] = []

        def _unexpected_remote_command(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("discovery should reuse the supplied SSH command")

        def _fake_run(cmd, *_args, **_kwargs):
            seen_cmds.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(
                    {
                        "directories": ["output", "output/empty"],
                        "files": ["output/kept/artifact.txt"],
                    }
                ),
                stderr="",
            )

        collector._run_remote_command = _unexpected_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_run):
            directories, files = collector._discover_copy_link_filelist(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir="/data/experiments/exp-42",
                experiment_filestore=tmp_path,
                known_hosts_path=None,
                ssh_user="test-user",
                symlink_relpaths=[Path("output")],
                ssh_command="ssh -p 2222 -o StrictHostKeyChecking=no",
                remote_host="127.0.0.1",
            )

        assert seen_cmds
        ssh_cmd = seen_cmds[0]
        assert ssh_cmd[:5] == [
            "ssh",
            "-p",
            "2222",
            "-o",
            "StrictHostKeyChecking=no",
        ]
        assert ssh_cmd[-2] == "test-user@127.0.0.1"
        assert ssh_cmd[-1].startswith("sudo python3 -c ")
        assert directories == [Path("output"), Path("output/empty")]
        assert files == ["output/kept/artifact.txt"]


class TestRsyncPreservesMtimes:
    """test_rsync_preserves_mtimes — ARTF-02."""

    def test_rsync_preserves_mtimes(self) -> None:
        """The built rsync command includes -a (archive mode, which preserves mtimes)."""
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        assert "-a" in cmd, "rsync must use -a (archive) flag to preserve mtimes"


# ---------------------------------------------------------------------------
# ARTF-03: staging and publish
# ---------------------------------------------------------------------------


class TestStagingAndPublish:
    """test_staging_and_publish — ARTF-03: staged tree published to final path."""

    def test_staging_and_publish(self, tmp_path: Path) -> None:
        """After successful rsync+verification, artifacts appear in final path; staging is cleaned."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        # Simulate the source tree that rsync would copy
        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(source_root, experiment_name="exp-42")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            """Copy source_root/exp-42 contents into the staging_dir."""
            # The staging dir is the last argument to rsync
            staging_dest = Path(cmd[-1].rstrip("/"))
            shutil.copytree(
                source_root / "exp-42",
                staging_dest,
                dirs_exist_ok=True,
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        # Artifacts must be in the final path
        assert final_path.exists(), "Final path must exist after collection"
        assert final_path == experiment_filestore / "exp-42"

        # At least one trial-N dir must be present
        trial_dirs = list(final_path.rglob("trial-*"))
        assert trial_dirs, "Final path must contain trial directories"

        # Staging directory must be gone
        staging_root = experiment_filestore / ".collect-staging"
        if staging_root.exists():
            worker_staging = staging_root / worker.name
            assert not worker_staging.exists(), (
                "Worker staging dir should be cleaned up"
            )

    def test_staging_and_publish_excludes_internal_oss_crs_workdir(
        self, tmp_path: Path
    ) -> None:
        """Collection should rehydrate output symlinks without restoring bulk logs."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        shutil.rmtree(trial_dir / "output")
        workdir_out = trial_dir / "oss-crs-workdir" / "out"
        (workdir_out / "seeds").mkdir(parents=True)
        (workdir_out / "seeds" / "seed-0001").write_bytes(b"seed-content")
        (workdir_out / "logs" / "services").mkdir(parents=True)
        (workdir_out / "logs" / "services" / "service.log").write_text("service log\n")
        (
            workdir_out
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).write_text("  [test] 12.3s\n")
        (
            workdir_out / "logs" / "services" / "crs-codex_inc-builder-asan.stdout.log"
        ).write_text("Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n")
        timing_dir = workdir_out / "logs" / "crs" / "builder-sidecar-lite" / "log_dir"
        timing_dir.mkdir(parents=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps({"rebuild": 70.4, "test": 481.0, "status": "pass"})
        )
        staged_dir = trial_dir / "staged" / "curl-delta-01"
        staged_dir.mkdir(parents=True)
        (staged_dir / "README.txt").write_text("staged leak target\n", encoding="utf-8")
        kept_dir = workdir_out / "kept"
        kept_dir.mkdir(parents=True)
        (kept_dir / "artifact.txt").symlink_to(
            Path("..") / ".." / ".." / "staged" / "curl-delta-01" / "README.txt"
        )
        (trial_dir / "result.log").symlink_to(Path("oss-crs-workdir") / "result.log")
        (trial_dir / "log-echo.txt").symlink_to(
            Path("oss-crs-workdir") / "log-echo.txt"
        )
        (trial_dir / "oss-crs-workdir" / "result.log").symlink_to(
            Path("..") / "staged" / "curl-delta-01" / "README.txt"
        )
        (trial_dir / "oss-crs-workdir" / "log-echo.txt").symlink_to(
            Path("..")
            / "output"
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        )
        (trial_dir / "output").symlink_to(Path("oss-crs-workdir") / "out")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_output = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
        )
        assert not any(path.name == "oss-crs-workdir" for path in final_path.rglob("*"))
        assert trial_output.exists()
        assert not trial_output.is_symlink()
        assert (trial_output / "seeds" / "seed-0001").exists()
        assert not (trial_output / "kept" / "artifact.txt").exists()
        assert not (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "result.log"
        ).exists()
        assert not (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "log-echo.txt"
        ).exists()
        assert not (trial_output / "logs" / "services" / "service.log").exists()
        assert (
            trial_output
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()
        assert (
            trial_output / "logs" / "services" / "crs-codex_inc-builder-asan.stdout.log"
        ).exists()
        assert (
            trial_output
            / "logs"
            / "crs"
            / "builder-sidecar-lite"
            / "log_dir"
            / "verify_patch_timing.json"
        ).exists()

    def test_staging_and_publish_excludes_trial_staged_dir(
        self, tmp_path: Path
    ) -> None:
        """Collection should skip trial-local staged benchmark copies."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for staged-dir regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        staged_dir = trial_dir / "staged" / "curl-delta-01"
        staged_dir.mkdir(parents=True)
        (staged_dir / "README.txt").write_text("temporary staged benchmark copy\n")
        staged_output_logs = staged_dir / "output" / "logs" / "services"
        staged_output_logs.mkdir(parents=True)
        (staged_output_logs / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "staged patcher log\n", encoding="utf-8"
        )
        (trial_dir / "staged-link.txt").symlink_to(
            Path("staged") / "curl-delta-01" / "README.txt"
        )
        shutil.rmtree(trial_dir / "output")
        (trial_dir / "output").symlink_to(Path("staged") / "curl-delta-01" / "output")
        nested_staged_dir = trial_dir / "notes" / "staged" / "keepme"
        nested_staged_dir.mkdir(parents=True)
        (nested_staged_dir / "info.txt").write_text("preserve nested staged content\n")
        (trial_dir / "nested-staged-link.txt").symlink_to(
            Path("notes") / "staged" / "keepme" / "info.txt"
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _unexpected_remote_command(*args: object, **kwargs: object) -> object:
            raise AssertionError(
                f"unexpected remote command: args={args!r}, kwargs={kwargs!r}"
            )

        collector._run_remote_command = _unexpected_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        collected_trial = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert (collected_trial / "metadata.json").exists()
        assert not (collected_trial / "staged").exists()
        assert not (collected_trial / "staged-link.txt").exists()
        assert not (collected_trial / "output").exists()
        assert (
            collected_trial / "notes" / "staged" / "keepme" / "info.txt"
        ).read_text() == "preserve nested staged content\n"
        preserved_nested_symlink = collected_trial / "nested-staged-link.txt"
        assert preserved_nested_symlink.is_symlink()
        assert (
            preserved_nested_symlink.resolve(strict=True).read_text()
            == "preserve nested staged content\n"
        )

    def test_staging_and_publish_rehydrates_absolute_workdir_symlink(
        self, tmp_path: Path
    ) -> None:
        """Collection should rehydrate absolute top-level symlinks into oss-crs-workdir."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for absolute-symlink coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        shutil.rmtree(trial_dir / "output")
        workdir_out = trial_dir / "oss-crs-workdir" / "out"
        (workdir_out / "seeds").mkdir(parents=True)
        (workdir_out / "seeds" / "seed-0001").write_bytes(b"seed-content")
        logs_dir = workdir_out / "logs" / "services"
        logs_dir.mkdir(parents=True)
        (logs_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "  [test] 12.3s\n", encoding="utf-8"
        )
        (trial_dir / "output").symlink_to(workdir_out.resolve())

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        collected_output = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
        )
        assert collected_output.exists()
        assert not collected_output.is_symlink()
        assert (collected_output / "seeds" / "seed-0001").exists()
        assert (
            collected_output
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()

    def test_staging_and_publish_excludes_output_logs_from_real_output_tree(
        self, tmp_path: Path
    ) -> None:
        """Collection should keep reporting logs while dropping unrelated output/log bulk."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        logs_dir = trial_dir / "output" / "logs" / "services"
        logs_dir.mkdir(parents=True)
        (logs_dir / "service.log").write_text("service log\n")
        (logs_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "  [test] 12.3s\n"
        )
        (logs_dir / "crs-codex_inc-builder-asan.stdout.log").write_text(
            "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n"
        )
        timing_dir = (
            trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite" / "log_dir"
        )
        timing_dir.mkdir(parents=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps({"rebuild": 70.4, "test": 481.0, "status": "pass"})
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_output = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
        )
        assert (trial_output / "seeds" / "seed-0001").exists()
        assert not (trial_output / "logs" / "services" / "service.log").exists()
        assert (
            trial_output
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()
        assert (
            trial_output / "logs" / "services" / "crs-codex_inc-builder-asan.stdout.log"
        ).exists()
        assert (
            trial_output
            / "logs"
            / "crs"
            / "builder-sidecar-lite"
            / "log_dir"
            / "verify_patch_timing.json"
        ).exists()

    def test_staging_and_publish_compacts_failed_trials_to_diagnostics_only(
        self, tmp_path: Path
    ) -> None:
        """Failed trials should publish only marker/metadata/worker-log diagnostics."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for failed-trial collection coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        (trial_dir / ".fail").write_text("", encoding="utf-8")
        (trial_dir / "llm-usage.json").write_text(
            json.dumps({"total_cost_usd": 1.23, "request_count": 7}),
            encoding="utf-8",
        )
        (trial_dir / "result.json").write_text("{}", encoding="utf-8")
        logs_dir = trial_dir / "output" / "logs" / "services"
        logs_dir.mkdir(parents=True)
        (logs_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "  [test] 12.3s\n",
            encoding="utf-8",
        )
        timing_dir = (
            trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite" / "log_dir"
        )
        timing_dir.mkdir(parents=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps({"rebuild": 70.4, "test": 481.0, "status": "fail"}),
            encoding="utf-8",
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        failed_trial = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert (failed_trial / "metadata.json").exists()
        assert (failed_trial / ".fail").exists()
        assert (failed_trial / "worker.log").exists()
        assert not (failed_trial / "snapshot-0001.tar.gz").exists()
        assert not (failed_trial / "snapshot-0001.complete").exists()
        assert not (failed_trial / "llm-usage.json").exists()
        assert not (failed_trial / "result.json").exists()
        assert not (failed_trial / "output" / "seeds").exists()
        assert (
            failed_trial
            / "output"
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()
        assert (
            failed_trial
            / "output"
            / "logs"
            / "crs"
            / "builder-sidecar-lite"
            / "log_dir"
            / "verify_patch_timing.json"
        ).exists()

    def test_staging_and_publish_replaces_existing_failed_trial_contents_on_recollect(
        self, tmp_path: Path
    ) -> None:
        """Re-collecting a failed trial should drop stale bulky files from earlier publishes."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for failed-trial collection coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        existing_trial = _build_trial_tree(
            experiment_filestore, experiment_name="exp-42"
        )
        (existing_trial / ".fail").write_text("", encoding="utf-8")
        (existing_trial / "output" / "seeds" / "stale-seed").write_bytes(b"stale-seed")
        (existing_trial / "old-artifact.txt").write_text(
            "stale artifact\n", encoding="utf-8"
        )

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        (trial_dir / ".fail").write_text("", encoding="utf-8")
        logs_dir = trial_dir / "output" / "logs" / "services"
        logs_dir.mkdir(parents=True)
        (logs_dir / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "  [test] 12.3s\n",
            encoding="utf-8",
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        failed_trial = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert not (failed_trial / "old-artifact.txt").exists()
        assert not (failed_trial / "snapshot-0001.tar.gz").exists()
        assert not (failed_trial / "output" / "seeds").exists()
        assert (
            failed_trial
            / "output"
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()

    def test_staging_and_publish_replaces_existing_successful_trial_contents_on_recollect(
        self, tmp_path: Path
    ) -> None:
        """Re-collecting a successful trial should drop previously leaked staged content."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for successful-trial collection coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        existing_trial = _build_trial_tree(
            experiment_filestore, experiment_name="exp-42"
        )
        stale_staged_dir = existing_trial / "staged" / "stale-copy"
        stale_staged_dir.mkdir(parents=True)
        (stale_staged_dir / "README.txt").write_text("stale staged content\n")
        other_trial = _build_trial_tree(
            experiment_filestore, experiment_name="exp-42", trial_n=2
        )

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(source_root, experiment_name="exp-42")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        refreshed_trial = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert not (refreshed_trial / "staged").exists()
        assert (refreshed_trial / "output" / "seeds" / "seed-0001").exists()
        assert other_trial.exists()

    def test_report_log_rsync_skips_internal_workdir_files(
        self, tmp_path: Path
    ) -> None:
        """Report-log copy should not restore oss-crs-workdir artifacts."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")

        report_logs = trial_dir / "output" / "logs" / "services"
        report_logs.mkdir(parents=True, exist_ok=True)
        (report_logs / "builder-sidecar-lite_patcher.stdout.log").write_text(
            "[keep] legit log\n"
        )
        (report_logs / "crs-codex_inc-builder-asan.stdout.log").write_text(
            "[keep] legit log\n"
        )
        staged_dir = trial_dir / "staged" / "curl-delta-01"
        staged_dir.mkdir(parents=True)
        (staged_dir / "linked_patcher.stdout.log").write_text(
            "[drop] staged-linked log\n", encoding="utf-8"
        )
        (report_logs / "linked_patcher.stdout.log").symlink_to(
            Path("..")
            / ".."
            / ".."
            / "staged"
            / "curl-delta-01"
            / "linked_patcher.stdout.log"
        )

        internal_services = (
            trial_dir / "oss-crs-workdir" / "run" / "output" / "logs" / "services"
        )
        internal_services.mkdir(parents=True, exist_ok=True)
        (internal_services / "bad_patcher.stdout.log").write_text("[drop] bad log\n")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_output = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
        )
        assert (
            trial_output
            / "logs"
            / "services"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()
        assert (
            trial_output / "logs" / "services" / "crs-codex_inc-builder-asan.stdout.log"
        ).exists()
        assert not (
            trial_output / "logs" / "services" / "linked_patcher.stdout.log"
        ).exists()
        assert not any(path.name == "oss-crs-workdir" for path in final_path.rglob("*"))
        assert not (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "oss-crs-workdir"
        ).exists()

    def test_staging_and_publish_preserves_legacy_reporting_logs_under_crs_tree(
        self, tmp_path: Path
    ) -> None:
        """Collection should retain report-critical logs stored under the legacy CRS tree."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        legacy_logs_dir = trial_dir / "output" / "logs" / "crs" / "builder-sidecar-lite"
        (legacy_logs_dir / "nested" / "patcher").mkdir(parents=True)
        (legacy_logs_dir / "nested" / "builders").mkdir(parents=True)
        (
            legacy_logs_dir
            / "nested"
            / "patcher"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).write_text("  [test] 12.3s\n")
        (
            legacy_logs_dir
            / "nested"
            / "builders"
            / "crs-codex_inc-builder-asan.stdout.log"
        ).write_text("Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n")
        timing_dir = legacy_logs_dir / "log_dir"
        timing_dir.mkdir(parents=True)
        (timing_dir / "verify_patch_timing.json").write_text(
            json.dumps({"rebuild": 70.4, "test": 481.0, "status": "pass"})
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        legacy_output_logs = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
            / "logs"
            / "crs"
            / "builder-sidecar-lite"
        )
        assert (
            legacy_output_logs
            / "nested"
            / "patcher"
            / "builder-sidecar-lite_patcher.stdout.log"
        ).exists()
        assert (
            legacy_output_logs
            / "nested"
            / "builders"
            / "crs-codex_inc-builder-asan.stdout.log"
        ).exists()
        assert (legacy_output_logs / "log_dir" / "verify_patch_timing.json").exists()

    def test_staging_and_publish_rehydrates_file_symlinks_into_excluded_workdir(
        self, tmp_path: Path
    ) -> None:
        """Collection should copy top-level file symlinks whose targets live under excluded dirs."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        workdir = trial_dir / "oss-crs-workdir"
        workdir.mkdir()
        (workdir / "result.log").write_text("log content\n")
        (trial_dir / "result.log").symlink_to(Path("oss-crs-workdir") / "result.log")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        result_log = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "result.log"
        )
        assert not any(path.name == "oss-crs-workdir" for path in final_path.rglob("*"))
        assert result_log.read_text() == "log content\n"
        assert not result_log.is_symlink()

    def test_staging_and_publish_rehydrates_top_level_symlink_without_following_cycle(
        self, tmp_path: Path
    ) -> None:
        """Collection should materialize a top-level excluded-dir symlink without descending into nested cycles.

        Current behavior fails during rehydration because rsync ``--copy-links``
        follows the nested loop until it hits ``ELOOP``; the assertions below
        define the intended post-fix outcome.
        """
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        shutil.rmtree(trial_dir / "output")
        workdir_out = trial_dir / "oss-crs-workdir" / "out"
        (workdir_out / "kept").mkdir(parents=True)
        (workdir_out / "kept" / "artifact.txt").write_text("artifact content\n")
        (workdir_out / "empty").mkdir(parents=True)
        (workdir_out / "nested" / "empty").mkdir(parents=True)
        (workdir_out / "cycle").symlink_to(Path())
        (trial_dir / "output").symlink_to(Path("oss-crs-workdir") / "out")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del args
            return _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_output = (
            final_path
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "output"
        )
        artifact_paths = sorted(
            path.relative_to(trial_output)
            for path in trial_output.rglob("artifact.txt")
        )

        assert not any(path.name == "oss-crs-workdir" for path in final_path.rglob("*"))
        assert trial_output.exists()
        assert not trial_output.is_symlink()
        assert (trial_output / "empty").is_dir()
        assert (trial_output / "nested" / "empty").is_dir()
        assert (
            trial_output / "kept" / "artifact.txt"
        ).read_text() == "artifact content\n"
        assert artifact_paths == [Path("kept") / "artifact.txt"]

    def test_staging_and_publish_fails_when_rehydrated_manifest_entries_vanish(
        self, tmp_path: Path
    ) -> None:
        """Collection should fail if a rehydrated symlink tree changes after manifest discovery."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for output/log regression coverage")

        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        shutil.rmtree(trial_dir / "output")
        workdir_out = trial_dir / "oss-crs-workdir" / "out"
        (workdir_out / "kept").mkdir(parents=True)
        artifact_path = workdir_out / "kept" / "artifact.txt"
        artifact_path.write_text("artifact content\n")
        (trial_dir / "output").symlink_to(Path("oss-crs-workdir") / "out")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()
        manifest_recorded = False

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            return _run_local_rsync_from_cloud_cmd(
                cmd,
                source_root=source_root,
                experiment_name="exp-42",
            )

        def _fake_remote_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal manifest_recorded
            del args
            result = _run_local_remote_command_from_cloud_cmd(
                str(kwargs["command"]),
                source_root=source_root,
                experiment_name="exp-42",
            )
            if not manifest_recorded:
                manifest_recorded = True
                artifact_path.unlink()
            return result

        collector._run_remote_command = _fake_remote_command  # type: ignore[method-assign]
        with patch("subprocess.run", side_effect=_fake_rsync):
            with pytest.raises(
                ArtifactCollectionError,
                match="manifest entries vanished during transfer",
            ):
                collector.collect(
                    worker=worker,
                    fleet=fleet,
                    experiment_name="exp-42",
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir="/data/experiments/exp-42",
                )

    def test_collect_reports_start_time_observation_from_staging(
        self, tmp_path: Path
    ) -> None:
        """Successful collection should report current-run start-time metadata from staging."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        _write_trial_metadata(
            trial_dir,
            {
                "timestamp_start": "2026-03-11T09:00:00+00:00",
                "timestamp": "2026-03-10T09:00:00+00:00",
                "trial_num": 1,
                "crs": "oss-crs",
                "benchmark": "curl-delta-01",
                "harness": "fuzz_http",
                "mode": "bug_finding",
                "source": {"path": "/src/curl", "commit": "abc123"},
            },
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()
        start_time_observations: list[tuple[str | None, str]] = []

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            staging_dest = Path(cmd[-1].rstrip("/"))
            shutil.copytree(
                source_root / "exp-42",
                staging_dest,
                dirs_exist_ok=True,
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=_fake_rsync):
            collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
                start_time_observations=start_time_observations,
            )

        assert start_time_observations == [
            ("2026-03-11T09:00:00+00:00", "earliest_trial_timestamp_start")
        ]

    def test_publish_overwrites_read_only_files_and_preserves_broken_symlinks(
        self, tmp_path: Path
    ) -> None:
        """Local publish should behave like rsync: preserve symlinks and replace prior read-only files."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync is required for publish regression coverage")

        staging_dir = tmp_path / "staging"
        final_dir = tmp_path / "final"
        staging_dir.mkdir()
        final_dir.mkdir()

        shared_rel = (
            Path("trial-1") / "oss-crs-workdir" / "crs_compose" / "crs_src" / "repo"
        )
        staging_repo = staging_dir / shared_rel
        final_repo = final_dir / shared_rel
        staging_repo.mkdir(parents=True, exist_ok=True)
        final_repo.mkdir(parents=True, exist_ok=True)

        readonly_dest = final_repo / "artifact.txt"
        readonly_dest.write_text("old\n")
        readonly_dest.chmod(0o444)

        updated_src = staging_repo / "artifact.txt"
        updated_src.write_text("new\n")
        import os

        dest_stat = readonly_dest.stat()
        os.utime(
            updated_src,
            (dest_stat.st_atime + 5, dest_stat.st_mtime + 5),
        )

        broken_link = staging_repo / "broken-link"
        broken_link.symlink_to("../missing-target")

        collector = ArtifactCollector()
        collector._publish(staging_dir, final_dir)

        assert not staging_dir.exists()
        assert readonly_dest.read_text() == "new\n"
        assert broken_link.name in {p.name for p in final_repo.iterdir()}
        published_link = final_repo / "broken-link"
        assert published_link.is_symlink()
        assert published_link.readlink() == Path("../missing-target")


class TestPartialStagingNotPublished:
    """test_partial_staging_not_published — ARTF-03: partial syncs never reach final path."""

    def test_partial_staging_not_published(self, tmp_path: Path) -> None:
        """If staged tree has no valid trials (missing metadata.json), publish is NOT called."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        # Build a trial tree WITHOUT metadata.json
        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(
            source_root, experiment_name="exp-bad", include_metadata=False
        )

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            staging_dest = Path(cmd[-1].rstrip("/"))
            shutil.copytree(
                source_root / "exp-bad",
                staging_dest,
                dirs_exist_ok=True,
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=_fake_rsync):
            with pytest.raises(ArtifactCollectionError):
                collector.collect(
                    worker=worker,
                    fleet=fleet,
                    experiment_name="exp-bad",
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir="/data/experiments/exp-bad",
                )

        # Final path must NOT be populated
        final_path = experiment_filestore / "exp-bad"
        if final_path.exists():
            trial_dirs = list(final_path.rglob("trial-*"))
            assert not trial_dirs, (
                "No trial dirs must be published when staging fails verification"
            )


# ---------------------------------------------------------------------------
# ARTF-04: reporting compatibility
# ---------------------------------------------------------------------------


class TestReportingCompat:
    """test_reporting_compat — ARTF-04: discover_trials works on collected layout."""

    def test_reporting_compat(self, tmp_path: Path) -> None:
        """discover_trials called on the published directory returns non-empty TrialInfo list."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(source_root, experiment_name="exp-42")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            staging_dest = Path(cmd[-1].rstrip("/"))
            shutil.copytree(
                source_root / "exp-42",
                staging_dest,
                dirs_exist_ok=True,
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("subprocess.run", side_effect=_fake_rsync):
            final_path = collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trials = discover_trials(final_path)
        assert trials, (
            "discover_trials must return at least one trial from the collected layout"
        )

        # Each trial must have metadata
        for trial in trials:
            assert trial.trial_dir.exists()


# ---------------------------------------------------------------------------
# ARTF-01: full trial tree (no include/exclude filters dropping .complete etc.)
# ---------------------------------------------------------------------------


class TestCollectFullTrialTree:
    """test_collect_full_trial_tree — ARTF-01: rsync source covers the full experiment subtree."""

    def test_collect_full_trial_tree(self) -> None:
        """rsync keeps full-tree behavior while excluding only internal scratch/log paths."""
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        cmd_str = " ".join(cmd)
        assert "--include" not in cmd_str, (
            "No --include filters; rsync must copy full tree"
        )
        exclude_args = [arg for arg in cmd if arg.startswith("--exclude=")]
        assert exclude_args == [
            "--exclude=oss-crs-workdir/",
            "--exclude=trial-*/staged/",
            "--exclude=output/logs/",
        ], (
            "Artifact collection should exclude only internal scratch data, staged benchmark copies, and trial output/logs"
        )

        # Source must end with trailing slash (rsync convention for directory contents)
        source = cmd[-2]
        assert source.endswith("/"), "rsync source must have trailing slash"


class TestRemoteLogCollection:
    """Best-effort remote log collection should land under the local cloud state dir."""

    def test_collect_logs_writes_service_and_trial_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        collector = ArtifactCollector(base_path=config_path)

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        (trial_dir / ".fail").write_text("", encoding="utf-8")

        def _fake_remote_command(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout="remote output\n",
                stderr="",
            )

        def _fake_subprocess_run(cmd, *_args, **_kwargs):
            if cmd and cmd[:3] == ["gcloud", "compute", "os-login"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="test-user\n",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keygen":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keyscan":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"{worker.external_ip} ssh-ed25519 AAAATESTKEY\n",
                    stderr="",
                )

            if cmd and cmd[0] == "rsync":
                dest = Path(cmd[-1].rstrip("/"))
                shutil.copytree(
                    source_root / "exp-42",
                    dest,
                    dirs_exist_ok=True,
                )
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        monkeypatch.setattr(collector, "_run_remote_command", _fake_remote_command)
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            logs_dir = collector.collect_logs(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        instance_dir = logs_dir / worker.name
        assert (instance_dir / "runtime-summary.txt").read_text(encoding="utf-8")
        assert (instance_dir / "google-startup-scripts.journal.log").read_text(
            encoding="utf-8"
        )
        assert (instance_dir / "google-guest-agent.journal.log").read_text(
            encoding="utf-8"
        )
        assert (instance_dir / "crsbench-worker.journal.log").read_text(
            encoding="utf-8"
        )
        assert (
            instance_dir
            / "trial-artifacts"
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "worker.log"
        ).read_text(encoding="utf-8") == (trial_dir / "worker.log").read_text(
            encoding="utf-8"
        )
        assert (
            instance_dir
            / "trial-artifacts"
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / ".fail"
        ).exists()

    def test_collect_logs_excludes_trial_staged_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        collector = ArtifactCollector(base_path=config_path)

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        staged_dir = trial_dir / "staged" / "curl-delta-01"
        staged_dir.mkdir(parents=True)
        (staged_dir / "metadata.json").write_text(
            '{"timestamp": "2026-03-13T00:00:00Z"}',
            encoding="utf-8",
        )
        (staged_dir / "worker.log").write_text("staged worker log\n", encoding="utf-8")
        stale_trial_artifacts_dir = (
            collector._remote_logs_dir(experiment_filestore, "exp-42")
            / worker.name
            / "trial-artifacts"
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
            / "staged"
            / "stale-copy"
        )
        stale_trial_artifacts_dir.mkdir(parents=True, exist_ok=True)
        (stale_trial_artifacts_dir / "worker.log").write_text(
            "stale staged log\n", encoding="utf-8"
        )

        def _fake_remote_command(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout="remote output\n",
                stderr="",
            )

        def _fake_subprocess_run(cmd, *_args, **_kwargs):
            if cmd and cmd[:3] == ["gcloud", "compute", "os-login"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="test-user\n",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keygen":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keyscan":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"{worker.external_ip} ssh-ed25519 AAAATESTKEY\n",
                    stderr="",
                )

            if cmd and cmd[0] == "rsync":
                return _run_local_rsync_from_cloud_cmd(
                    cmd,
                    source_root=source_root,
                    experiment_name="exp-42",
                )

            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        monkeypatch.setattr(collector, "_run_remote_command", _fake_remote_command)
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            logs_dir = collector.collect_logs(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_artifacts_dir = (
            logs_dir
            / worker.name
            / "trial-artifacts"
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert (trial_artifacts_dir / "worker.log").exists()
        assert not (trial_artifacts_dir / "staged").exists()

    def test_collect_logs_drops_top_level_symlinks_into_trial_staged_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        collector = ArtifactCollector(base_path=config_path)

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        staged_dir = trial_dir / "staged" / "curl-delta-01"
        staged_dir.mkdir(parents=True)
        (staged_dir / "worker.log").write_text("staged worker log\n", encoding="utf-8")
        (trial_dir / "worker.log").unlink()
        (trial_dir / "worker.log").symlink_to(
            Path("staged") / "curl-delta-01" / "worker.log"
        )

        def _fake_remote_command(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout="remote output\n",
                stderr="",
            )

        def _fake_subprocess_run(cmd, *_args, **_kwargs):
            if cmd and cmd[:3] == ["gcloud", "compute", "os-login"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="test-user\n",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keygen":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )

            if cmd and cmd[0] == "ssh-keyscan":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"{worker.external_ip} ssh-ed25519 AAAATESTKEY\n",
                    stderr="",
                )

            if cmd and cmd[0] == "rsync":
                return _run_local_rsync_from_cloud_cmd(
                    cmd,
                    source_root=source_root,
                    experiment_name="exp-42",
                )

            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        monkeypatch.setattr(collector, "_run_remote_command", _fake_remote_command)
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            logs_dir = collector.collect_logs(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        trial_artifacts_dir = (
            logs_dir
            / worker.name
            / "trial-artifacts"
            / "oss-crs"
            / "curl-delta-01"
            / "fuzz_http"
            / "delta"
            / "address"
            / "trial-1"
        )
        assert not (trial_artifacts_dir / "worker.log").exists()
        assert (trial_artifacts_dir / "metadata.json").exists()

    def test_collect_logs_raises_when_remote_command_transport_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        collector = ArtifactCollector(base_path=config_path)

        def _fake_remote_command(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=255,
                stdout="",
                stderr="ssh: connect to host failed",
            )

        def _fake_subprocess_run(cmd, *_args, **_kwargs):
            if cmd and cmd[:3] == ["gcloud", "compute", "os-login"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="test-user\n",
                    stderr="",
                )

            if cmd and cmd[0] in {"ssh-keygen", "ssh-keyscan"}:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="ssh-ed25519 AAAATESTKEY\n",
                    stderr="",
                )

            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        monkeypatch.setattr(collector, "_run_remote_command", _fake_remote_command)
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            with pytest.raises(ArtifactCollectionError):
                collector.collect_logs(
                    worker=worker,
                    fleet=fleet,
                    experiment_name="exp-42",
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir="/data/experiments/exp-42",
                )

    def test_collect_logs_uses_evaluator_service_name_for_evaluator_instances(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = _make_worker(name="gce-evaluator-001")
        worker.labels["crsbench-role"] = "evaluator"
        fleet = _make_fleet(ssh_via_iap=False)
        config_path = tmp_path / "config.yaml"
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        collector = ArtifactCollector(base_path=config_path)

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(source_root, experiment_name="exp-42")

        def _fake_remote_command(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout="remote output\n",
                stderr="",
            )

        def _fake_subprocess_run(cmd, *_args, **_kwargs):
            if cmd and cmd[:3] == ["gcloud", "compute", "os-login"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="test-user\n",
                    stderr="",
                )

            if cmd and cmd[0] in {"ssh-keygen", "ssh-keyscan"}:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="ssh-ed25519 AAAATESTKEY\n",
                    stderr="",
                )

            if cmd and cmd[0] == "rsync":
                dest = Path(cmd[-1].rstrip("/"))
                shutil.copytree(
                    source_root / "exp-42",
                    dest,
                    dirs_exist_ok=True,
                )
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        monkeypatch.setattr(collector, "_run_remote_command", _fake_remote_command)
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            logs_dir = collector.collect_logs(
                worker=worker,
                fleet=fleet,
                experiment_name="exp-42",
                experiment_filestore=experiment_filestore,
                remote_experiment_dir="/data/experiments/exp-42",
            )

        instance_dir = logs_dir / worker.name
        assert (instance_dir / "crsbench-evaluator.journal.log").read_text(
            encoding="utf-8"
        )

    def test_run_remote_command_iap_uses_local_ssh_transport(
        self, tmp_path: Path
    ) -> None:
        """IAP-backed remote commands should go through a local tunnel and config-scoped known_hosts."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        calls: list[list[str]] = []

        class _Tunnel:
            def __enter__(self):
                return 2222

            def __exit__(self, exc_type, exc, tb):
                del exc_type, exc, tb
                return False

        def _fake_run(cmd, *_args, **_kwargs):
            calls.append(cmd)
            if cmd and cmd[0] == "ssh-keygen":
                return subprocess.CompletedProcess(args=cmd, returncode=0)
            if cmd and cmd[0] == "ssh":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="remote output\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected subprocess invocation: {cmd!r}")

        with (
            patch.object(collector, "_open_iap_tunnel", return_value=_Tunnel()),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            result = collector._run_remote_command(
                worker=worker,
                fleet=fleet,
                known_hosts_path=None,
                ssh_user="test-user",
                command="echo hello",
                experiment_filestore=experiment_filestore,
            )

        assert result.returncode == 0
        known_hosts_path = tmp_path / ".crsbench-cloud" / "known_hosts_iap"
        assert calls[0] == [
            "ssh-keygen",
            "-R",
            worker.name,
            "-f",
            str(known_hosts_path),
        ]
        ssh_cmd = calls[1]
        assert ssh_cmd[0] == "ssh"
        assert "StrictHostKeyChecking=no" in ssh_cmd
        assert f"HostKeyAlias={worker.name}" in ssh_cmd
        assert f"UserKnownHostsFile={known_hosts_path}" in ssh_cmd
        assert "127.0.0.1" in ssh_cmd
        assert "echo hello" in ssh_cmd

    def test_rehydrate_excluded_symlink_entries_iap_reuses_single_tunnel(
        self, tmp_path: Path
    ) -> None:
        """IAP symlink rehydration should share one tunnel across per-root discovery calls."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector(base_path=tmp_path / "config.yaml")
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        discovery_calls: list[tuple[str | None, str | None, list[Path]]] = []

        class _Tunnel:
            def __enter__(self):
                return 2222

            def __exit__(self, exc_type, exc, tb):
                del exc_type, exc, tb
                return False

        def _fake_discover(
            *args: object, **kwargs: object
        ) -> tuple[list[Path], list[str]]:
            del args
            symlink_relpaths = list(kwargs["symlink_relpaths"])  # type: ignore[index]
            discovery_calls.append(
                (
                    kwargs.get("ssh_command"),  # type: ignore[arg-type]
                    kwargs.get("remote_host"),  # type: ignore[arg-type]
                    symlink_relpaths,
                )
            )
            return [symlink_relpaths[0]], []

        with (
            patch.object(
                collector,
                "_prepare_iap_known_hosts",
                return_value=tmp_path / "known_hosts_iap",
            ),
            patch.object(
                collector, "_open_iap_tunnel", return_value=_Tunnel()
            ) as open_tunnel,
            patch.object(
                collector,
                "_discover_copy_link_filelist",
                side_effect=_fake_discover,
            ),
            patch.object(collector, "_run_copy_link_filelist_rsync"),
            patch.object(collector, "_verify_rehydrated_copy_link_manifest"),
        ):
            collector._rehydrate_excluded_symlink_entries(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir="/data/experiments/exp-42",
                staging_dir=staging_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=None,
                ssh_user="test-user",
                symlink_relpaths=[Path("output"), Path("results")],
            )

        open_tunnel.assert_called_once_with(worker=worker, fleet=fleet)
        assert len(discovery_calls) == 2
        for ssh_command, remote_host, symlink_relpaths in discovery_calls:
            assert ssh_command is not None
            assert "-p 2222" in ssh_command
            assert remote_host == "127.0.0.1"
            assert len(symlink_relpaths) == 1


# ---------------------------------------------------------------------------
# Collect marker helpers
# ---------------------------------------------------------------------------


class TestCollectMarker:
    """test_collect_marker — collect marker metadata read/write round-trip."""

    def test_collect_marker_roundtrip(self, tmp_path: Path) -> None:
        """Collect marker write/read round-trip preserves a hidden marker payload."""
        destination = tmp_path / "exp-42"
        payload: dict[str, object] = {
            "schema_version": 1,
            "experiment_name": "exp-42",
            "local_destination": str(destination),
            "last_collect_time": "2026-03-20T12:00:00+00:00",
            "experiment_start_time": "2026-03-20T11:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp_start",
        }

        write_collect_marker(destination, payload)
        assert (
            collect_marker_path(destination) == destination / ".crsbench-collect.json"
        )
        assert read_collect_marker(destination) == payload

    def test_read_collect_marker_returns_none_for_malformed_json(
        self, tmp_path: Path
    ) -> None:
        """Malformed `.crsbench-collect.json` files must be treated as missing markers."""
        destination = tmp_path / "exp-42"
        destination.mkdir(parents=True, exist_ok=True)
        marker_path = collect_marker_path(destination)
        marker_path.write_text("{bad json", encoding="utf-8")

        assert read_collect_marker(destination) is None

    def test_write_collect_marker_replace_failure_preserves_prior_marker(
        self,
        tmp_path: Path,
    ) -> None:
        """Atomic marker updates should not corrupt the prior marker on replace failure."""
        destination = tmp_path / "exp-42"
        prior_payload: dict[str, object] = {
            "schema_version": 1,
            "experiment_name": "exp-42",
            "local_destination": str(destination),
            "last_collect_time": "2026-03-20T12:00:00+00:00",
            "experiment_start_time": "2026-03-20T11:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp_start",
        }
        updated_payload: dict[str, object] = {
            **prior_payload,
            "last_collect_time": "2026-03-21T12:00:00+00:00",
        }
        write_collect_marker(destination, prior_payload)

        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                write_collect_marker(destination, updated_payload)

        assert read_collect_marker(destination) == prior_payload


class TestExperimentStartTimeDiscovery:
    """test_experiment_start_time — start time inference from current staging trees."""

    def test_discover_experiment_start_time_prefers_earliest_timestamp_start(
        self, tmp_path: Path
    ) -> None:
        """When timestamp_start exists, use the earliest value and source it."""
        stage_a = tmp_path / "stage-a"
        stage_b = tmp_path / "stage-b"

        trial_a = _build_trial_tree(stage_a, experiment_name="exp-42", trial_n=1)
        trial_b = _build_trial_tree(stage_a, experiment_name="exp-42", trial_n=2)
        _write_trial_metadata(
            trial_a,
            {
                "timestamp_start": "2026-03-12T10:00:00+00:00",
                "timestamp": "2026-03-11T10:00:00+00:00",
            },
        )
        _write_trial_metadata(
            trial_b,
            {
                "timestamp_start": "2026-03-11T09:00:00+00:00",
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )
        trial_c = _build_trial_tree(stage_b, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial_c,
            {
                "timestamp": "2026-03-09T08:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging(
            [stage_a, stage_b]
        )
        assert start_time == "2026-03-11T09:00:00+00:00"
        assert source == "earliest_trial_timestamp_start"

    def test_discover_experiment_start_time_falls_back_to_legacy_timestamp(
        self, tmp_path: Path
    ) -> None:
        """Without timestamp_start fields, fallback to earliest legacy timestamp."""
        stage_a = tmp_path / "stage-a"
        stage_b = tmp_path / "stage-b"

        trial_a = _build_trial_tree(stage_a, experiment_name="exp-42", trial_n=1)
        trial_b = _build_trial_tree(stage_a, experiment_name="exp-42", trial_n=2)
        _write_trial_metadata(
            trial_a,
            {
                "timestamp": "2026-03-11T10:00:00+00:00",
            },
        )
        _write_trial_metadata(
            trial_b,
            {
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )
        trial_c = _build_trial_tree(stage_b, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial_c,
            {
                "timestamp": "2026-03-12T08:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging(
            [stage_a, stage_b]
        )
        assert start_time == "2026-03-10T09:00:00+00:00"
        assert source == "earliest_trial_timestamp"

    def test_discover_experiment_start_time_accepts_numeric_timestamp_start(
        self, tmp_path: Path
    ) -> None:
        """Numeric timestamp_start values from orchestrator-written metadata are accepted."""
        stage = tmp_path / "stage-a"
        trial = _build_trial_tree(stage, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial,
            {
                "timestamp_start": 1710012345.25,
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging([stage])
        assert start_time == "2024-03-09T19:25:45.250000+00:00"
        assert source == "earliest_trial_timestamp_start"

    def test_discover_experiment_start_time_ignores_boolean_timestamp_start(
        self, tmp_path: Path
    ) -> None:
        """Boolean metadata values are not coerced into bogus epoch timestamps."""
        stage = tmp_path / "stage-a"
        trial = _build_trial_tree(stage, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial,
            {
                "timestamp_start": True,
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging([stage])
        assert start_time == "2026-03-10T09:00:00+00:00"
        assert source == "earliest_trial_timestamp"

    def test_discover_experiment_start_time_ignores_invalid_numeric_timestamp_start(
        self, tmp_path: Path
    ) -> None:
        """Non-finite numeric metadata is treated as absent."""
        stage = tmp_path / "stage-a"
        trial = _build_trial_tree(stage, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial,
            {
                "timestamp_start": float("nan"),
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging([stage])
        assert start_time == "2026-03-10T09:00:00+00:00"
        assert source == "earliest_trial_timestamp"

    def test_discover_experiment_start_time_ignores_out_of_range_timestamp_start(
        self, tmp_path: Path
    ) -> None:
        """Out-of-range numeric metadata is treated as absent."""
        stage = tmp_path / "stage-a"
        trial = _build_trial_tree(stage, experiment_name="exp-42", trial_n=1)
        _write_trial_metadata(
            trial,
            {
                "timestamp_start": 10**30,
                "timestamp": "2026-03-10T09:00:00+00:00",
            },
        )

        start_time, source = discover_experiment_start_time_from_staging([stage])
        assert start_time == "2026-03-10T09:00:00+00:00"
        assert source == "earliest_trial_timestamp"

    def test_discover_experiment_start_time_returns_unknown_when_missing(
        self, tmp_path: Path
    ) -> None:
        """Missing timestamp fields returns an unknown start-time tuple."""
        stage_a = tmp_path / "stage-a"
        stage_b = tmp_path / "stage-b"

        trial_a = _build_trial_tree(
            stage_a, experiment_name="exp-42", trial_n=1, include_metadata=False
        )
        trial_b = _build_trial_tree(
            stage_b, experiment_name="exp-42", trial_n=1, include_metadata=False
        )
        _write_trial_metadata(trial_a, {"status": "complete"})
        _write_trial_metadata(trial_b, {"status": "complete"})

        start_time, source = discover_experiment_start_time_from_staging(
            [stage_a, stage_b]
        )
        assert start_time is None
        assert source == "unknown"


class TestMergeExperimentStartTime:
    """test_merge_experiment_start_time — preserve prior marker data as fallback."""

    def test_merge_experiment_start_time_prefers_current_run_value(self) -> None:
        """Current-run start time must win when both current and prior values exist."""
        prior_marker = {
            "schema_version": 1,
            "experiment_name": "exp-42",
            "experiment_start_time": "2026-03-19T05:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp",
        }
        current = ("2026-03-20T06:00:00+00:00", "earliest_trial_timestamp_start")

        start_time, source = merge_experiment_start_time(
            current=current,
            prior=prior_marker,
        )
        assert start_time == "2026-03-20T06:00:00+00:00"
        assert source == "earliest_trial_timestamp_start"

    def test_merge_experiment_start_time_preserves_prior_marker_when_current_unknown(
        self,
    ) -> None:
        """Prior marker values are preserved if current run has unknown start time."""
        prior_marker = {
            "schema_version": 1,
            "experiment_name": "exp-42",
            "experiment_start_time": "2026-03-19T05:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp",
        }
        current = (None, "unknown")

        start_time, source = merge_experiment_start_time(
            current=current,
            prior=prior_marker,
        )
        assert start_time == "2026-03-19T05:00:00+00:00"
        assert source == "earliest_trial_timestamp"
