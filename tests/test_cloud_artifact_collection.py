"""Tests for ArtifactCollector — ARTF-01 through ARTF-04."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from crsbench.cloud.collection import ArtifactCollectionError, ArtifactCollector
from crsbench.cloud.gce.models import GceWorkerRecord
from crsbench.reporting.snapshot_loader import discover_trials
from crsbench.validation.schemas import GceWorkerFleetConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    name: str = "gce-worker-001",
    internal_ip: str = "10.0.0.10",
    zone: str = "us-central1-a",
) -> GceWorkerRecord:
    return GceWorkerRecord(
        name=name,
        instance_id="1001",
        status="RUNNING",
        zone=zone,
        internal_ip=internal_ip,
        external_ip=None,
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        labels={},
        raw={},
    )


def _make_fleet(ssh_via_iap: bool = False, zone: str = "us-central1-a") -> GceWorkerFleetConfig:
    return GceWorkerFleetConfig(
        project="test-project",
        zone=zone,
        ssh_via_iap=ssh_via_iap,
    )


def _build_trial_tree(
    base: Path,
    experiment_name: str = "exp-42",
    crs: str = "oss-crs",
    benchmark: str = "curl-delta-01",
    harness: str = "fuzz_http",
    trial_n: int = 1,
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
        meta.write_text(json.dumps({"trial": trial_n, "crs": crs}))

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


# ---------------------------------------------------------------------------
# ARTF-01 / Task tests: rsync command construction
# ---------------------------------------------------------------------------


class TestRsyncCmdIap:
    """test_rsync_cmd_iap — ARTF-01: IAP SSH transport."""

    def test_rsync_cmd_iap(self) -> None:
        """When ssh_via_iap=True the -e arg uses gcloud compute ssh with --tunnel-through-iap."""
        worker = _make_worker(zone="us-central1-a")
        fleet = _make_fleet(ssh_via_iap=True, zone="us-central1-a")
        collector = ArtifactCollector()

        cmd = collector._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir="/data/experiments/exp-42",
            staging_dir=Path("/tmp/staging"),
        )

        # Must be a list of strings
        assert isinstance(cmd, list)
        assert all(isinstance(token, str) for token in cmd)

        # rsync is the executable
        assert cmd[0] == "rsync"

        # -e flag must be present and contain the IAP gcloud invocation
        assert "-e" in cmd
        e_idx = cmd.index("-e")
        ssh_cmd = cmd[e_idx + 1]
        assert "gcloud" in ssh_cmd
        assert "compute" in ssh_cmd
        assert "ssh" in ssh_cmd
        assert worker.name in ssh_cmd
        assert "--project=test-project" in ssh_cmd
        assert "--zone=us-central1-a" in ssh_cmd
        assert "--tunnel-through-iap" in ssh_cmd
        assert "-W %h:%p" in ssh_cmd

        # Required rsync flags
        assert "-a" in cmd
        assert "--mkpath" in cmd
        assert "--partial-dir=.rsync-partial" in cmd
        assert "--delay-updates" in cmd
        assert "--delete-delay" in cmd


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

        def _fake_rsync(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
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
            assert not worker_staging.exists(), "Worker staging dir should be cleaned up"


class TestPartialStagingNotPublished:
    """test_partial_staging_not_published — ARTF-03: partial syncs never reach final path."""

    def test_partial_staging_not_published(self, tmp_path: Path) -> None:
        """If staged tree has no valid trials (missing metadata.json), publish is NOT called."""
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        # Build a trial tree WITHOUT metadata.json
        source_root = tmp_path / "worker-local"
        source_root.mkdir()
        _build_trial_tree(source_root, experiment_name="exp-bad", include_metadata=False)

        worker = _make_worker()
        fleet = _make_fleet(ssh_via_iap=False)
        collector = ArtifactCollector()

        def _fake_rsync(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
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
            assert not trial_dirs, "No trial dirs must be published when staging fails verification"


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

        def _fake_rsync(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:  # type: ignore[type-arg]
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
        assert trials, "discover_trials must return at least one trial from the collected layout"

        # Each trial must have metadata
        for trial in trials:
            assert trial.trial_dir.exists()


# ---------------------------------------------------------------------------
# ARTF-01: full trial tree (no include/exclude filters dropping .complete etc.)
# ---------------------------------------------------------------------------


class TestCollectFullTrialTree:
    """test_collect_full_trial_tree — ARTF-01: rsync source covers the full experiment subtree."""

    def test_collect_full_trial_tree(self) -> None:
        """rsync command has NO include/exclude filters that would drop .complete markers, seeds, logs."""
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
        assert "--include" not in cmd_str, "No --include filters; rsync must copy full tree"
        assert "--exclude" not in cmd_str, "No --exclude filters; rsync must copy full tree"

        # Source must end with trailing slash (rsync convention for directory contents)
        source = cmd[-2]
        assert source.endswith("/"), "rsync source must have trailing slash"
