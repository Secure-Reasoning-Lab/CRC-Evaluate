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
            inc_build_enabled=False,
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
        assert restored.inc_build_enabled is False
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
        assert reg.worker_cores_per_job is None
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
        assert reg.cores_per_trial is None
        assert reg.memory_per_trial is None
        assert reg.worker_cores_per_job is None

    def test_from_experiment_config_honors_inc_build_enabled(self) -> None:
        """Registration preserves experiment inc-build enablement for pre-build hints."""
        from crsbench.distributed.registry import RuntimeRegistration

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.worker = None
        config.evaluator = None
        config.inc_build_enabled = False
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
        config.model_dump.return_value = {
            "experiment": "exp",
            "inc_build_enabled": False,
        }

        reg = RuntimeRegistration.from_experiment_config(config)

        assert reg.inc_build_enabled is False

    def test_from_experiment_config_evaluator_unified_defaults(self) -> None:
        """Unified evaluator jobs/cores_per_job populate build+verify metadata."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.validation.schemas import EvaluatorConfig

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.worker = None
        config.evaluator = EvaluatorConfig(jobs=6, cores_per_job=8)
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

        reg = RuntimeRegistration.from_experiment_config(config)
        assert reg.evaluator_build_jobs == 6
        assert reg.evaluator_verify_jobs == 6
        assert reg.evaluator_build_cores_per_job == 8
        assert reg.evaluator_verify_cores_per_job == 8

    def test_from_experiment_config_evaluator_split_overrides_unified(self) -> None:
        """Split evaluator values override unified defaults per queue role."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.validation.schemas import EvaluatorConfig

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.worker = None
        config.evaluator = EvaluatorConfig(
            jobs=6,
            cores_per_job=8,
            verify_jobs=3,
            verify_cores_per_job=5,
        )
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

        reg = RuntimeRegistration.from_experiment_config(config)
        assert reg.evaluator_build_jobs == 6
        assert reg.evaluator_build_cores_per_job == 8
        assert reg.evaluator_verify_jobs == 3
        assert reg.evaluator_verify_cores_per_job == 5

    def test_from_experiment_config_evaluator_build_override_falls_back_verify(
        self,
    ) -> None:
        """Build overrides do not break verify fallback to unified defaults."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.validation.schemas import EvaluatorConfig

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.worker = None
        config.evaluator = EvaluatorConfig(
            jobs=6,
            cores_per_job=8,
            build_jobs=10,
            build_cores_per_job=12,
        )
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

        reg = RuntimeRegistration.from_experiment_config(config)
        assert reg.evaluator_build_jobs == 10
        assert reg.evaluator_build_cores_per_job == 12
        assert reg.evaluator_verify_jobs == 6
        assert reg.evaluator_verify_cores_per_job == 8

    def test_from_experiment_config_evaluator_verify_override_falls_back_build(
        self,
    ) -> None:
        """Verify overrides do not break build fallback to unified defaults."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.validation.schemas import EvaluatorConfig

        config = MagicMock()
        config.experiment = "exp"
        config.mode.value = "delta"
        config.sanitizers = []
        config.resources = None
        config.worker = None
        config.evaluator = EvaluatorConfig(
            jobs=6,
            cores_per_job=8,
            verify_jobs=3,
            verify_cores_per_job=5,
        )
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

        reg = RuntimeRegistration.from_experiment_config(config)
        assert reg.evaluator_build_jobs == 6
        assert reg.evaluator_build_cores_per_job == 8
        assert reg.evaluator_verify_jobs == 3
        assert reg.evaluator_verify_cores_per_job == 5


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
        mock_conn.set.return_value = True
        mock_conn.eval.return_value = 1

        assert client.lock("my-exp") is True
        client.unlock("my-exp")
        assert "my-exp" not in client._lock_tokens


class TestRegistryLease:
    """Test RegistryLease lifecycle behavior."""

    def _make_client(self):
        """Create a RegistryClient with a mock Redis connection."""
        from crsbench.distributed.registry import RegistryClient

        mock_conn = MagicMock()
        return RegistryClient(mock_conn), mock_conn

    def test_cleanup_deregisters_and_unlocks(self) -> None:
        from crsbench.distributed.registry import (
            _LOCK_UNLOCK_SCRIPT,
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
        token = client._lock_tokens["exp-test"]
        lease.register(reg)
        lease.cleanup()

        mock_conn.hdel.assert_called_once_with(
            "crsbench:registry:experiments", "exp-test"
        )
        mock_conn.eval.assert_any_call(
            _LOCK_UNLOCK_SCRIPT,
            1,
            "crsbench:lock:exp-test",
            token,
        )

    def test_cleanup_without_registration_only_unlocks(self) -> None:
        from crsbench.distributed.registry import (
            _LOCK_UNLOCK_SCRIPT,
            RegistryClient,
            RegistryLease,
        )

        mock_conn = MagicMock()
        mock_conn.set.return_value = True
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-test")

        assert lease.acquire_lock() is True
        token = client._lock_tokens["exp-test"]
        lease.cleanup()

        mock_conn.hdel.assert_not_called()
        mock_conn.eval.assert_any_call(
            _LOCK_UNLOCK_SCRIPT,
            1,
            "crsbench:lock:exp-test",
            token,
        )

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
        mock_conn.eval.side_effect = RuntimeError("redis down")
        client = RegistryClient(mock_conn)
        lease = RegistryLease(client, "exp-test")
        assert lease.acquire_lock() is True
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

        assert mock_conn.set.call_count == 1
        args, kwargs = mock_conn.set.call_args
        assert args[0] == "crsbench:lock:exp-42"
        assert isinstance(args[1], str)
        assert args[1]
        assert kwargs == {"nx": True, "ex": LOCK_TTL}

    def test_renew_returns_true_when_key_exists(self) -> None:
        """renew() returns True and extends TTL when the lock exists."""
        from crsbench.distributed.registry import _LOCK_RENEW_SCRIPT, LOCK_TTL

        client, mock_conn = self._make_client()
        mock_conn.set.return_value = True
        mock_conn.eval.return_value = 1

        assert client.lock("exp-42") is True
        assert client.renew("exp-42") is True
        mock_conn.eval.assert_called_with(
            _LOCK_RENEW_SCRIPT,
            1,
            "crsbench:lock:exp-42",
            client._lock_tokens["exp-42"],
            str(LOCK_TTL),
        )

    def test_renew_returns_false_when_key_missing(self) -> None:
        """renew() returns False when the lock has expired."""
        client, mock_conn = self._make_client()
        mock_conn.set.return_value = True
        mock_conn.eval.return_value = 0

        assert client.lock("exp-42") is True
        assert client.renew("exp-42") is False

    def test_unlock_uses_compare_and_delete(self) -> None:
        """unlock() uses token compare-and-delete Lua script."""
        from crsbench.distributed.registry import _LOCK_UNLOCK_SCRIPT

        client, mock_conn = self._make_client()
        mock_conn.set.return_value = True
        mock_conn.eval.return_value = 1

        assert client.lock("exp-42") is True
        token = client._lock_tokens["exp-42"]
        client.unlock("exp-42")

        mock_conn.eval.assert_called_with(
            _LOCK_UNLOCK_SCRIPT,
            1,
            "crsbench:lock:exp-42",
            token,
        )

    def test_stale_owner_renew_fails_with_token_mismatch(self) -> None:
        """stale owner must not renew lock after another owner acquires it."""
        from crsbench.distributed.registry import RegistryClient

        client_a, conn_a = self._make_client()
        client_b = RegistryClient(conn_a)
        conn_a.set.return_value = True
        conn_a.eval.side_effect = [0]

        assert client_a.lock("exp-42") is True
        assert client_b.lock("exp-42") is True
        assert client_a.renew("exp-42") is False

    def test_stale_owner_unlock_does_not_delete_new_owner_lock(self) -> None:
        """stale owner unlock must not delete lock held by a newer owner."""
        from crsbench.distributed.registry import _LOCK_UNLOCK_SCRIPT, RegistryClient

        client_a, conn_a = self._make_client()
        client_b = RegistryClient(conn_a)
        conn_a.set.return_value = True
        conn_a.eval.side_effect = [0]

        assert client_a.lock("exp-42") is True
        assert client_b.lock("exp-42") is True
        stale_token = client_a._lock_tokens["exp-42"]
        client_a.unlock("exp-42")

        conn_a.eval.assert_called_with(
            _LOCK_UNLOCK_SCRIPT,
            1,
            "crsbench:lock:exp-42",
            stale_token,
        )
