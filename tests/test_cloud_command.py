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


@pytest.fixture()
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
        fake.hset(f"crsbench:cloud:workers:{experiment}", f"id-worker-{i}", json.dumps(w))

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


def _mock_config(has_cloud: bool = True):
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
        args = parser.parse_args(["cloud", "status", "my-exp", "--config", "c.yaml", "--json"])
        assert args.json_output is True

    def test_parse_events(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "events", "my-exp", "--config", "c.yaml"])
        assert args.cloud_command == "events"
        assert args.event_type is None

    def test_parse_events_with_type(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "events", "my-exp", "--config", "c.yaml", "--type", "orphan_detected"]
        )
        assert args.event_type == "orphan_detected"

    def test_parse_teardown(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "teardown", "my-exp", "--config", "c.yaml"])
        assert args.cloud_command == "teardown"
        assert args.force is False

    def test_parse_teardown_force(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "teardown", "my-exp", "--config", "c.yaml", "--force"])
        assert args.force is True

    def test_parse_collect(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "collect", "my-exp", "--config", "c.yaml"])
        assert args.cloud_command == "collect"
