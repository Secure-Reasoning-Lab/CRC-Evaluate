"""Tests for the Redis experiment registry.

Tests:
1. RuntimeRegistration serialization roundtrip
2. RuntimeRegistration.from_experiment_config()
3. RegistryClient register/deregister/list/get operations
4. Pub/sub event publishing
5. Corrupt entry handling
6. Empty registry
"""

import json
from unittest.mock import MagicMock

import pytest


class TestRuntimeRegistration:
    """Test RuntimeRegistration model."""

    def test_default_version(self) -> None:
        """Default version is 1."""
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(experiment="test-exp")
        assert reg.version == 1

    def test_serialization_roundtrip(self) -> None:
        """Model serializes to JSON and back without data loss."""
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="my-experiment",
            trial_queue="crsbench_my-experiment",
            build_queue="crsbench_my-experiment_build",
            verify_queue="crsbench_my-experiment_verify",
            cores_per_trial=8,
            memory_per_trial="16G",
            benchmarks=["bench-a", "bench-b"],
            sanitizers=["address"],
            modes=["delta"],
            benchmarks_root="/data/benchmarks",
            max_total_time=3600,
            build_timeout=1800,
            per_pov_verify_timeout=120,
            config_hash="abc123",
        )

        json_str = reg.model_dump_json()
        restored = RuntimeRegistration.model_validate_json(json_str)

        assert restored.experiment == "my-experiment"
        assert restored.cores_per_trial == 8
        assert restored.memory_per_trial == "16G"
        assert restored.benchmarks == ["bench-a", "bench-b"]
        assert restored.config_hash == "abc123"

    def test_extra_fields_ignored(self) -> None:
        """Unknown fields are silently ignored (forward compat)."""
        from crsbench.distributed.registry import RuntimeRegistration

        data = {
            "experiment": "test",
            "trial_queue": "q",
            "build_queue": "q_build",
            "verify_queue": "q_verify",
            "future_field": "should-be-ignored",
        }
        reg = RuntimeRegistration.model_validate(data)
        assert reg.experiment == "test"
        assert not hasattr(reg, "future_field")

    def test_from_experiment_config(self) -> None:
        """from_experiment_config extracts correct fields from ExperimentConfig."""
        from crsbench.distributed.queue import resolve_queue_names
        from crsbench.distributed.registry import RuntimeRegistration

        # Create a mock ExperimentConfig
        config = MagicMock()
        config.experiment = "exp-42"
        config.mode.value = "delta"
        config.sanitizers = [MagicMock(value="address"), MagicMock(value="memory")]
        config.resources = MagicMock()
        config.resources.cores_per_trial = 8
        config.resources.memory_per_trial = "16G"
        config.oss_fuzz_path = "/opt/oss-fuzz"
        config.benchmarks_root = "/data/benchmarks"
        config.source_mode = "pkgs"
        config.inc_image_policy = "auto"
        config.inc_image_registry = "ghcr.io/team-atlanta/crsbench"
        config.inc_image_max_pull_bytes = 10 * 1024 * 1024 * 1024
        config.inc_image_pull_timeout_sec = 300
        config.project_image_prefix = "crsbench"
        config.max_total_time = 7200
        config.build_timeout = 3600
        config.per_pov_verify_timeout = 180
        config.get_benchmark_list.return_value = ["bench-a", "bench-b"]
        config.model_dump.return_value = {"experiment": "exp-42"}

        reg = RuntimeRegistration.from_experiment_config(config)
        trial_queue, build_queue, verify_queue = resolve_queue_names("exp-42")

        assert reg.experiment == "exp-42"
        assert reg.trial_queue == trial_queue
        assert reg.build_queue == build_queue
        assert reg.verify_queue == verify_queue
        assert reg.cores_per_trial == 8
        assert reg.memory_per_trial == "16G"
        assert reg.benchmarks == ["bench-a", "bench-b"]
        assert reg.sanitizers == ["address", "memory"]
        assert reg.modes == ["delta"]
        assert reg.config_hash  # Non-empty hash

    def test_config_hash_is_stable(self) -> None:
        """Same config produces the same hash."""
        from crsbench.distributed.registry import RuntimeRegistration

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.oss_fuzz_path = "/oss-fuzz"
        config.benchmarks_root = "/benchmarks"
        config.source_mode = "pkgs"
        config.inc_image_policy = "auto"
        config.inc_image_registry = "ghcr.io/team-atlanta/crsbench"
        config.inc_image_max_pull_bytes = 10 * 1024 * 1024 * 1024
        config.inc_image_pull_timeout_sec = 300
        config.project_image_prefix = "crsbench"
        config.max_total_time = 7200
        config.build_timeout = 3600
        config.per_pov_verify_timeout = 180
        config.get_benchmark_list.return_value = []
        config.model_dump.return_value = {"experiment": "exp", "mode": "delta"}

        reg1 = RuntimeRegistration.from_experiment_config(config)
        reg2 = RuntimeRegistration.from_experiment_config(config)

        assert reg1.config_hash == reg2.config_hash

    def test_from_config_no_resources(self) -> None:
        """from_experiment_config handles None resources gracefully."""
        from crsbench.distributed.registry import RuntimeRegistration

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "all"
        config.sanitizers = []
        config.resources = None
        config.oss_fuzz_path = "oss-fuzz"
        config.benchmarks_root = "benchmarks"
        config.source_mode = "pkgs"
        config.inc_image_policy = "auto"
        config.inc_image_registry = "ghcr.io/team-atlanta/crsbench"
        config.inc_image_max_pull_bytes = 10 * 1024 * 1024 * 1024
        config.inc_image_pull_timeout_sec = 300
        config.project_image_prefix = "crsbench"
        config.max_total_time = 7200
        config.build_timeout = 3600
        config.per_pov_verify_timeout = 180
        config.get_benchmark_list.return_value = []
        config.model_dump.return_value = {}

        reg = RuntimeRegistration.from_experiment_config(config)
        assert reg.cores_per_trial == 4  # Default
        assert reg.memory_per_trial is None  # Unlimited by default


class TestRegistryClient:
    """Test RegistryClient CRUD operations using mocked Redis."""

    def _make_client(self):
        """Create a RegistryClient with a mock Redis connection."""
        from crsbench.distributed.registry import RegistryClient

        mock_conn = MagicMock()
        return RegistryClient(mock_conn), mock_conn

    def _make_registration(self, name: str = "exp-test"):
        """Create a sample RuntimeRegistration."""
        from crsbench.distributed.registry import RuntimeRegistration

        return RuntimeRegistration(
            experiment=name,
            trial_queue=f"crsbench_{name}",
            build_queue=f"crsbench_{name}_build",
            verify_queue=f"crsbench_{name}_verify",
        )

    def test_register_sets_hash_and_publishes(self) -> None:
        """register() calls HSET and PUBLISH."""
        client, mock_conn = self._make_client()
        reg = self._make_registration()

        client.register(reg)

        mock_conn.hset.assert_called_once()
        call_args = mock_conn.hset.call_args
        assert call_args[0][0] == "crsbench:registry:experiments"
        assert call_args[0][1] == "exp-test"

        mock_conn.publish.assert_called_once()
        pub_args = mock_conn.publish.call_args
        assert pub_args[0][0] == "crsbench:registry:events"
        event = json.loads(pub_args[0][1])
        assert event["event"] == "register"
        assert event["experiment"] == "exp-test"

    def test_deregister_deletes_and_publishes(self) -> None:
        """deregister() calls HDEL and PUBLISH."""
        client, mock_conn = self._make_client()

        client.deregister("exp-test")

        mock_conn.hdel.assert_called_once_with(
            "crsbench:registry:experiments", "exp-test"
        )
        mock_conn.publish.assert_called_once()
        event = json.loads(mock_conn.publish.call_args[0][1])
        assert event["event"] == "deregister"

    def test_list_experiments_parses_all(self) -> None:
        """list_experiments() parses all entries from HGETALL."""
        client, mock_conn = self._make_client()
        reg = self._make_registration("exp-a")

        mock_conn.hgetall.return_value = {
            b"exp-a": reg.model_dump_json().encode(),
        }

        result = client.list_experiments()
        assert "exp-a" in result
        assert result["exp-a"].experiment == "exp-a"

    def test_list_experiments_skips_corrupt(self) -> None:
        """list_experiments() skips entries that fail to parse."""
        client, mock_conn = self._make_client()

        mock_conn.hgetall.return_value = {
            b"good": self._make_registration("good").model_dump_json().encode(),
            b"bad": b"not-valid-json{{{",
        }

        result = client.list_experiments()
        assert "good" in result
        assert "bad" not in result

    def test_list_experiments_empty(self) -> None:
        """list_experiments() returns empty dict when registry is empty."""
        client, mock_conn = self._make_client()
        mock_conn.hgetall.return_value = {}

        result = client.list_experiments()
        assert result == {}

    def test_get_experiment_found(self) -> None:
        """get_experiment() returns registration when key exists."""
        client, mock_conn = self._make_client()
        reg = self._make_registration("exp-test")
        mock_conn.hget.return_value = reg.model_dump_json().encode()

        result = client.get_experiment("exp-test")
        assert result is not None
        assert result.experiment == "exp-test"

    def test_get_experiment_not_found(self) -> None:
        """get_experiment() returns None when key is missing."""
        client, mock_conn = self._make_client()
        mock_conn.hget.return_value = None

        result = client.get_experiment("nonexistent")
        assert result is None

    def test_get_experiment_corrupt(self) -> None:
        """get_experiment() returns None for corrupt entries."""
        client, mock_conn = self._make_client()
        mock_conn.hget.return_value = b"not-json"

        result = client.get_experiment("broken")
        assert result is None


class TestRegistryClientLock:
    """Test RegistryClient distributed lock operations."""

    def _make_client(self):
        """Create a RegistryClient with a mock Redis connection."""
        from crsbench.distributed.registry import RegistryClient

        mock_conn = MagicMock()
        return RegistryClient(mock_conn), mock_conn

    def test_lock_acquires_successfully(self) -> None:
        """lock() returns True when the key does not exist yet."""
        client, mock_conn = self._make_client()
        mock_conn.set.return_value = True

        assert client.lock("my-exp") is True

    def test_lock_fails_when_held(self) -> None:
        """lock() returns False when another orchestrator holds the lock."""
        client, mock_conn = self._make_client()
        mock_conn.set.return_value = None  # Redis SET NX returns None on miss

        assert client.lock("my-exp") is False

    def test_unlock_releases_lock(self) -> None:
        """After unlock(), a subsequent lock() can succeed."""
        client, mock_conn = self._make_client()
        # First call held, second call succeeds (simulates unlock then lock)
        mock_conn.set.side_effect = [None, True]

        assert client.lock("my-exp") is False
        client.unlock("my-exp")
        assert client.lock("my-exp") is True


class TestRegistryLease:
    """Test RegistryLease lifecycle behavior."""

    def _make_client(self):
        """Create a RegistryClient with a mock Redis connection."""
        from crsbench.distributed.registry import RegistryClient

        mock_conn = MagicMock()
        return RegistryClient(mock_conn), mock_conn

    def test_cleanup_deregisters_and_unlocks(self) -> None:
        from crsbench.distributed.registry import (
            RegistryClient,
            RegistryLease,
            RuntimeRegistration,
        )

        mock_conn = MagicMock()
        mock_conn.set.return_value = True
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-test")
        reg = RuntimeRegistration(experiment="exp-test")

        assert lease.acquire_lock() is True
        lease.register(reg)
        lease.cleanup()

        mock_conn.hdel.assert_called_once_with(
            "crsbench:registry:experiments", "exp-test"
        )
        mock_conn.delete.assert_called_once_with("crsbench:lock:exp-test")

    def test_cleanup_without_registration_only_unlocks(self) -> None:
        from crsbench.distributed.registry import RegistryClient, RegistryLease

        mock_conn = MagicMock()
        mock_conn.set.return_value = True
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-test")

        assert lease.acquire_lock() is True
        lease.cleanup()

        mock_conn.hdel.assert_not_called()
        mock_conn.delete.assert_called_once_with("crsbench:lock:exp-test")

    def test_register_rejects_mismatched_experiment(self) -> None:
        from crsbench.distributed.registry import (
            RegistryClient,
            RegistryLease,
            RuntimeRegistration,
        )

        mock_conn = MagicMock()
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-a")

        with pytest.raises(ValueError, match="Registration experiment mismatch"):
            lease.register(RuntimeRegistration(experiment="exp-b"))

    def test_cleanup_keeps_flags_when_redis_cleanup_fails(self) -> None:
        from crsbench.distributed.registry import RegistryClient, RegistryLease

        mock_conn = MagicMock()
        mock_conn.set.return_value = True
        mock_conn.hdel.side_effect = RuntimeError("redis down")
        mock_conn.delete.side_effect = RuntimeError("redis down")
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-test")
        lease.lock_acquired = True
        lease.registration_published = True

        lease.cleanup()

        assert lease.registration_published is True
        assert lease.lock_acquired is True

    def test_lock_uses_nx_and_ex(self) -> None:
        """lock() calls SET with NX and EX flags."""
        from crsbench.distributed.registry import LOCK_TTL

        client, mock_conn = self._make_client()
        mock_conn.set.return_value = True

        client.lock("exp-42")

        mock_conn.set.assert_called_once_with(
            "crsbench:lock:exp-42", "locked", nx=True, ex=LOCK_TTL
        )

    def test_renew_returns_true_when_key_exists(self) -> None:
        """renew() returns True and extends TTL when the lock exists."""
        from crsbench.distributed.registry import LOCK_TTL

        client, mock_conn = self._make_client()
        mock_conn.expire.return_value = True

        assert client.renew("exp-42") is True
        mock_conn.expire.assert_called_once_with("crsbench:lock:exp-42", LOCK_TTL)

    def test_renew_returns_false_when_key_missing(self) -> None:
        """renew() returns False when the lock has expired."""
        client, mock_conn = self._make_client()
        mock_conn.expire.return_value = False

        assert client.renew("exp-42") is False

    def test_unlock_deletes_key(self) -> None:
        """unlock() calls DELETE on the correct lock key."""
        client, mock_conn = self._make_client()

        client.unlock("exp-42")

        mock_conn.delete.assert_called_once_with("crsbench:lock:exp-42")
