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


def _rsync_exclude_patterns(cmd: list[str]) -> tuple[str, ...]:
    """Return normalized rsync exclude patterns from a command line."""
    return tuple(
        arg.removeprefix("--exclude=").rstrip("/")
        for arg in cmd
        if arg.startswith("--exclude=")
    )


def _copytree_with_rsync_excludes(
    source: Path,
    destination: Path,
    *,
    exclude_patterns: tuple[str, ...],
    symlinks: bool,
) -> None:
    """Copy a tree while honoring rsync-like exclude suffix matching."""
    source = source.resolve()

    def _ignore(directory: str, files: list[str]) -> set[str]:
        relative_dir = Path(directory).resolve().relative_to(source)
        ignored: set[str] = set()
        for name in files:
            relpath = (
                Path(name) if relative_dir == Path() else relative_dir / name
            ).as_posix()
            for pattern in exclude_patterns:
                if relpath == pattern or relpath.endswith(f"/{pattern}"):
                    ignored.add(name)
                    break
        return ignored

    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=_ignore,
        symlinks=symlinks,
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
        """Collection should rehydrate top-level symlinks into excluded oss-crs scratch dirs."""
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
        (trial_dir / "output").symlink_to(Path("oss-crs-workdir") / "out")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            """Copy source_root/exp-42 contents into staging, emulating rsync exclude/copy-links behavior."""
            source = cmd[-2]
            dest = Path(cmd[-1].rstrip("/"))
            if ":" in source:
                remote_source = source.split(":", 1)[1]
                rel = Path(
                    remote_source.removeprefix("/data/experiments/exp-42").lstrip("/")
                )
                local_source = source_root / "exp-42" / rel
            else:
                local_source = Path(source.rstrip("/"))

            exclude_patterns = _rsync_exclude_patterns(cmd)

            if "--copy-links" in cmd:
                resolved_source = local_source.resolve()
                if resolved_source.is_dir():
                    _copytree_with_rsync_excludes(
                        resolved_source,
                        dest / local_source.name,
                        exclude_patterns=exclude_patterns,
                        symlinks=False,
                    )
                else:
                    shutil.copy2(resolved_source, dest / local_source.name)
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            _copytree_with_rsync_excludes(
                local_source,
                dest,
                exclude_patterns=exclude_patterns,
                symlinks=True,
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
        assert not (trial_output / "logs").exists()

    def test_staging_and_publish_excludes_output_logs_from_real_output_tree(
        self, tmp_path: Path
    ) -> None:
        """Collection should omit real output/logs directories from the main artifact rsync."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        trial_dir = _build_trial_tree(source_root, experiment_name="exp-42")
        logs_dir = trial_dir / "output" / "logs" / "services"
        logs_dir.mkdir(parents=True)
        (logs_dir / "service.log").write_text("service log\n")

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(
            cmd: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
            source = cmd[-2]
            dest = Path(cmd[-1].rstrip("/"))
            if ":" in source:
                remote_source = source.split(":", 1)[1]
                rel = Path(
                    remote_source.removeprefix("/data/experiments/exp-42").lstrip("/")
                )
                local_source = source_root / "exp-42" / rel
            else:
                local_source = Path(source.rstrip("/"))

            exclude_patterns = _rsync_exclude_patterns(cmd)

            if "--copy-links" in cmd:
                resolved_source = local_source.resolve()
                if resolved_source.is_dir():
                    _copytree_with_rsync_excludes(
                        resolved_source,
                        dest / local_source.name,
                        exclude_patterns=exclude_patterns,
                        symlinks=False,
                    )
                else:
                    shutil.copy2(resolved_source, dest / local_source.name)
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            _copytree_with_rsync_excludes(
                local_source,
                dest,
                exclude_patterns=exclude_patterns,
                symlinks=True,
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
        assert not (trial_output / "logs").exists()

    def test_staging_and_publish_rehydrates_file_symlinks_into_excluded_workdir(
        self, tmp_path: Path
    ) -> None:
        """Collection should copy top-level file symlinks whose targets live under excluded dirs."""
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
            source = cmd[-2]
            dest = Path(cmd[-1].rstrip("/"))
            if ":" in source:
                remote_source = source.split(":", 1)[1]
                rel = Path(
                    remote_source.removeprefix("/data/experiments/exp-42").lstrip("/")
                )
                local_source = source_root / "exp-42" / rel
            else:
                local_source = Path(source.rstrip("/"))

            if "--copy-links" in cmd:
                resolved_source = local_source.resolve()
                if resolved_source.is_dir():
                    shutil.copytree(
                        resolved_source,
                        dest / local_source.name,
                        dirs_exist_ok=True,
                    )
                else:
                    shutil.copy2(resolved_source, dest / local_source.name)
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            excluded_names = {
                arg.removeprefix("--exclude=").rstrip("/")
                for arg in cmd
                if arg.startswith("--exclude=")
            }

            def _ignore(_directory: str, files: list[str]) -> set[str]:
                return {name for name in files if name in excluded_names}

            shutil.copytree(
                local_source,
                dest,
                dirs_exist_ok=True,
                ignore=_ignore,
                symlinks=True,
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
            "--exclude=output/logs/",
        ], (
            "Artifact collection should exclude only internal scratch data and trial output/logs"
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
