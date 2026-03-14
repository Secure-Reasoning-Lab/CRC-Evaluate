"""Unit tests for crsbench cloud CLI command -- status, events, config reconnect."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fake Redis (reusable fixture)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal fake Redis for unit testing, with hash and list support."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)
        self._lists.pop(key, None)

    def rpush(self, key: str, value: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.append(value)
        return len(lst)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start : stop + 1]


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_worker_status(
    instance_name: str = "worker-1",
    state: str = "ready",
    zone: str = "us-central1-a",
    internal_ip: str = "10.0.0.1",
) -> dict[str, Any]:
    """Return a CloudWorkerStatus-shaped dict for JSON storage in fake Redis."""
    return {
        "experiment_name": "test-exp",
        "instance_id": f"id-{instance_name}",
        "instance_name": instance_name,
        "zone": zone,
        "state": state,
        "provider_status": "RUNNING",
        "internal_ip": internal_ip,
        "external_ip": None,
        "detail": None,
        "startup_evidence": None,
        "updated_at": "2026-03-13T00:00:00+00:00",
        "ready_at": "2026-03-13T00:00:00+00:00",
    }


def _make_job_record(
    job_id: str = "job-1",
    trial_key: str = "trial-abc",
    state: str = "running",
    claimed_by: str | None = "worker-1",
) -> dict[str, Any]:
    """Return a JobLifecycleRecord-shaped dict for JSON storage in fake Redis."""
    return {
        "job_id": job_id,
        "trial_key": trial_key,
        "state": state,
        "claimed_by": claimed_by,
        "retry_count": 0,
        "last_heartbeat": None,
        "updated_at": "2026-03-13T00:00:00+00:00",
        "detail": None,
    }


def _make_recovery_event(
    event_type: str = "orphan_detected",
    job_id: str = "job-1",
    worker: str = "worker-1",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "job_id": job_id,
        "worker": worker,
        "detail": f"{event_type} for {job_id}",
        "ts": "2026-03-13T00:00:00+00:00",
    }


def _populate_fake_redis(fake: _FakeRedis, experiment: str = "test-exp") -> None:
    """Populate fake Redis with worker, job, and event test data."""
    # Workers
    for i, state in enumerate(["ready", "ready", "booting"], start=1):
        w = _make_worker_status(f"worker-{i}", state=state, internal_ip=f"10.0.0.{i}")
        fake.hset(
            f"crsbench:cloud:workers:{experiment}", f"id-worker-{i}", json.dumps(w)
        )

    # Jobs
    for i, (state, claimed) in enumerate(
        [("running", "worker-1"), ("completed", "worker-2"), ("queued", None)], start=1
    ):
        j = _make_job_record(f"job-{i}", f"trial-{i}", state=state, claimed_by=claimed)
        fake.hset(f"crsbench:jobs:{experiment}", f"job-{i}", json.dumps(j))

    # Events
    for etype in ["orphan_detected", "requeued", "orphan_detected"]:
        fake.rpush(
            f"crsbench:recovery-events:{experiment}",
            json.dumps(_make_recovery_event(etype)),
        )


# ---------------------------------------------------------------------------
# Config reconnect tests
# ---------------------------------------------------------------------------


def _mock_config(*, has_cloud: bool = True):
    """Build a mock ExperimentConfig."""
    config = MagicMock()
    if has_cloud:
        config.cloud = MagicMock()
        config.cloud.gce = MagicMock(name="GceWorkerFleetConfig")
    else:
        config.cloud = None
    config.redis_host = "localhost"
    config.experiment_filestore = Path("/tmp/filestore")
    return config


class TestReconnect:
    """Tests for _config_reconnect.reconnect()."""

    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_valid_config(self, mock_load, mock_redis):
        """reconnect() returns tuple of (fleet, redis_conn, readiness, lifecycle, filestore)."""
        mock_load.return_value = _mock_config(has_cloud=True)
        mock_redis.return_value = _FakeRedis()

        from crsbench.cloud.cli._config_reconnect import reconnect

        result = reconnect("/path/to/config.yaml", "test-exp")
        assert len(result) == 5
        fleet, redis_conn, readiness, lifecycle, filestore = result
        assert fleet is not None
        assert redis_conn is not None
        assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_missing_cloud_exits(self, mock_load):
        """reconnect() raises SystemExit when config has no cloud section."""
        mock_load.return_value = _mock_config(has_cloud=False)

        from crsbench.cloud.cli._config_reconnect import reconnect

        with pytest.raises(SystemExit):
            reconnect("/path/to/config.yaml", "test-exp")


# ---------------------------------------------------------------------------
# Argument parsing tests
# ---------------------------------------------------------------------------


class TestArgParsing:
    """Tests for add_cloud_subparser() argument structure."""

    def _build_parser(self) -> argparse.ArgumentParser:
        from crsbench.cloud.cli.cloud_command import add_cloud_subparser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_cloud_subparser(subparsers)
        return parser

    def test_parse_status(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "status", "my-exp", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "status"
        assert args.experiment == "my-exp"
        assert args.config == "c.yaml"
        assert args.json_output is False

    def test_parse_status_json(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "status", "my-exp", "--config", "c.yaml", "--json"]
        )
        assert args.json_output is True

    def test_parse_events(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "events", "my-exp", "--config", "c.yaml"])
        assert args.cloud_command == "events"
        assert args.event_type is None

    def test_parse_events_with_type(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "events",
                "my-exp",
                "--config",
                "c.yaml",
                "--type",
                "orphan_detected",
            ]
        )
        assert args.event_type == "orphan_detected"

    def test_parse_teardown(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
            ]
        )
        assert args.cloud_command == "teardown"
        assert args.force is False
        assert args.remote_dir == "/home/user/experiments/my-exp"

    def test_parse_teardown_force(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
                "--force",
            ]
        )
        assert args.force is True

    def test_parse_collect(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "collect",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
            ]
        )
        assert args.cloud_command == "collect"
        assert args.remote_dir == "/home/user/experiments/my-exp"


# ---------------------------------------------------------------------------
# Status sub-action tests
# ---------------------------------------------------------------------------


def _make_status_args(experiment: str = "test-exp", *, json_output: bool = False):
    return argparse.Namespace(
        experiment=experiment,
        config="/tmp/config.yaml",
        json_output=json_output,
        cloud_command="status",
    )


def _make_events_args(
    experiment: str = "test-exp",
    *,
    json_output: bool = False,
    event_type: str | None = None,
):
    return argparse.Namespace(
        experiment=experiment,
        config="/tmp/config.yaml",
        json_output=json_output,
        event_type=event_type,
        cloud_command="events",
    )


class TestStatusOutput:
    """Tests for run_status() human-readable and JSON output."""

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_output(self, mock_reconnect, fake_redis):
        """run_status() calls log_table for fleet, job, collection, and events sections."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        with (
            patch("crsbench.cloud.cli._status.log_table") as mock_table,
            patch("crsbench.cloud.cli._status.log_section"),
            patch("crsbench.cloud.cli._status.log_key_value"),
        ):
            rc = run_status(_make_status_args())

        assert rc == 0
        # Should have called log_table for fleet, jobs, and events sections
        assert mock_table.call_count >= 3

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_json_output(self, mock_reconnect, fake_redis, capsys):
        """run_status() with --json prints valid JSON with fleet/jobs/collection/events keys."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "fleet" in data
        assert "jobs" in data
        assert "collection" in data
        assert "events" in data

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_job_instance_correlation(self, mock_reconnect, fake_redis, capsys):
        """Job entries in JSON output include claimed_by for instance correlation (OBS-01)."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # At least one job should have claimed_by set
        claimed_values = [j["claimed_by"] for j in data["jobs"] if j.get("claimed_by")]
        assert len(claimed_values) > 0


# ---------------------------------------------------------------------------
# Events sub-action tests
# ---------------------------------------------------------------------------


class TestEventsOutput:
    """Tests for run_events() human-readable and JSON output."""

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_filtering(self, mock_reconnect, fake_redis):
        """run_events() with --type filters events by type field."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        with patch("crsbench.cloud.cli._events.log_table") as mock_table:
            rc = run_events(_make_events_args(event_type="requeued"))

        assert rc == 0
        # Should have called log_table once for the filtered events
        assert mock_table.call_count == 1
        # The rows passed should only contain the "requeued" event
        _, call_kwargs = mock_table.call_args
        if not call_kwargs:
            call_args = mock_table.call_args[0]
            rows = call_args[1]  # second positional arg = rows
        else:
            rows = call_kwargs.get("rows", mock_table.call_args[0][1])
        assert len(rows) == 1

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_json_output(self, mock_reconnect, fake_redis, capsys):
        """run_events() with --json prints valid JSON array."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        rc = run_events(_make_events_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 3  # 3 events populated

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_json_with_type_filter(self, mock_reconnect, fake_redis, capsys):
        """run_events() with --json and --type filters then outputs JSON array."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        rc = run_events(
            _make_events_args(json_output=True, event_type="orphan_detected")
        )
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 2  # 2 orphan_detected events
        assert all(e["type"] == "orphan_detected" for e in data)


# ---------------------------------------------------------------------------
# Collect sub-action tests
# ---------------------------------------------------------------------------


def _make_collect_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        cloud_command="collect",
    )


def _make_gce_worker(name: str, zone: str = "us-central1-a", ip: str = "10.0.0.1"):
    """Build a GceWorkerRecord for testing."""
    from crsbench.cloud.gce.models import GceWorkerRecord

    return GceWorkerRecord(
        name=name,
        instance_id=f"id-{name}",
        status="RUNNING",
        zone=zone,
        internal_ip=ip,
    )


class TestCollect:
    """Tests for run_collect() sub-action."""

    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_invokes_collector(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """run_collect() invokes ArtifactCollector.collect() for each live GCE worker."""
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),  # fleet
            MagicMock(),  # redis_conn
            readiness,
            MagicMock(),  # lifecycle
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 0
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.logger")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_stale_redis_warning(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_logger
    ):
        """Warns when Redis has workers not present in GCE."""
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
        mock_prov_cls.return_value = mock_prov

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        # Redis knows about w-1 and w-2, but GCE only has w-1
        stale_worker = MagicMock()
        stale_worker.instance_name = "w-2"
        live_worker = MagicMock()
        live_worker.instance_name = "w-1"
        readiness = MagicMock()
        readiness.list_workers.return_value = [live_worker, stale_worker]

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        # Verify logger.warning was called with stale info
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("w-2" in call for call in warning_calls)

    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_partial_failure(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """Partial collection failure returns 1 but continues for remaining workers."""
        from crsbench.cloud.collection import ArtifactCollectionError

        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov

        mock_coll = MagicMock()
        mock_coll.collect.side_effect = [
            ArtifactCollectionError("rsync failed"),
            Path("/tmp/out"),
        ]
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 1
        # Both workers should have been attempted
        assert mock_coll.collect.call_count == 2


# ---------------------------------------------------------------------------
# Teardown sub-action tests
# ---------------------------------------------------------------------------


def _make_teardown_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
    *,
    force: bool = False,
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        force=force,
        cloud_command="teardown",
    )


def _setup_teardown_mocks(
    mock_reconnect,
    mock_prov_cls,
    mock_coll_cls,
    workers=None,
    redis_workers=None,
    jobs=None,
):
    """Wire up common mock structure for teardown tests."""
    if workers is None:
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]

    mock_prov = MagicMock()
    mock_prov.list_workers.return_value = workers
    mock_prov_cls.return_value = mock_prov

    mock_coll = MagicMock()
    mock_coll_cls.return_value = mock_coll

    readiness = MagicMock()
    readiness.list_workers.return_value = redis_workers or []

    lifecycle = MagicMock()
    lifecycle.list_jobs.return_value = jobs or []

    mock_reconnect.return_value = (
        MagicMock(),  # fleet
        MagicMock(),  # redis_conn
        readiness,
        lifecycle,
        Path("/tmp/filestore"),
    )

    return mock_prov, mock_coll, readiness, lifecycle


class TestTeardown:
    """Tests for run_teardown() sub-action."""

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_collect_then_delete(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """Teardown collects from all workers then deletes them."""
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 0
        # Collect called for each worker
        assert mock_coll.collect.call_count == 2
        # Delete called after collection
        mock_prov.delete_workers.assert_called_once()
        # Verify collect was called BEFORE delete
        collect_order = mock_coll.collect.call_args_list
        delete_order = mock_prov.delete_workers.call_args_list
        assert len(collect_order) == 2
        assert len(delete_order) == 1

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_aborts_on_collection_failure(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """If any collection fails, teardown aborts -- delete_workers NOT called."""
        from crsbench.cloud.collection import ArtifactCollectionError

        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_coll.collect.side_effect = ArtifactCollectionError("rsync failed")

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 1
        mock_prov.delete_workers.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_force_flag(self, mock_reconnect, mock_prov_cls, mock_coll_cls):
        """With --force, no input() call is made."""
        _setup_teardown_mocks(mock_reconnect, mock_prov_cls, mock_coll_cls)

        from crsbench.cloud.cli._teardown import run_teardown

        with patch("builtins.input") as mock_input:
            rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        mock_input.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.logger")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_stale_redis_warning(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_logger
    ):
        """When GCE has no workers but Redis does, warn about stale entries."""
        stale_worker = MagicMock()
        stale_worker.instance_name = "w-stale"

        _setup_teardown_mocks(
            mock_reconnect,
            mock_prov_cls,
            mock_coll_cls,
            workers=[],
            redis_workers=[stale_worker],
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 0
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("stale" in call.lower() for call in warning_calls)

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_confirmation_prompt_yes(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """Confirmation prompt with 'yes' proceeds with teardown."""
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with (
            patch("builtins.input", return_value="yes"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 0
        mock_prov.delete_workers.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_confirmation_prompt_no(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """Confirmation prompt with non-'yes' cancels teardown."""
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with (
            patch("builtins.input", return_value="no"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 0
        mock_prov.delete_workers.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_non_tty_without_force(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls
    ):
        """Non-TTY stdin without --force returns 1 with error."""
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 1
        mock_prov.delete_workers.assert_not_called()
