"""Tests for Redis connection probing helpers."""

from unittest.mock import Mock, patch

import crsbench.distributed.queue as queue_module
import pytest


@patch.object(queue_module, "create_redis_connection")
def test_probe_redis_connection_reports_ready_on_success(
    mock_create_redis_connection: Mock,
) -> None:
    """Successful probes should report ready and close the connection."""
    connection = Mock()
    mock_create_redis_connection.return_value = connection

    probe_state, detail = queue_module.probe_redis_connection("redis.internal:6379")

    assert probe_state == queue_module.RedisConnectionProbe.READY
    assert detail is None
    connection.close.assert_called_once_with()


@patch.object(queue_module, "create_redis_connection")
def test_probe_redis_connection_reports_retryable_transport_failures(
    mock_create_redis_connection: Mock,
) -> None:
    """Connection and timeout failures should remain retryable."""
    mock_create_redis_connection.side_effect = queue_module.redis.ConnectionError(
        "connection refused"
    )

    probe_state, detail = queue_module.probe_redis_connection("redis.internal:6379")

    assert probe_state == queue_module.RedisConnectionProbe.RETRYABLE
    assert "connection refused" in detail


@patch.object(queue_module, "create_redis_connection")
def test_probe_redis_connection_reports_fatal_auth_failures(
    mock_create_redis_connection: Mock,
) -> None:
    """Authentication failures should not be retried as if Redis were still booting."""
    mock_create_redis_connection.side_effect = queue_module.redis.AuthenticationError(
        "bad password"
    )

    probe_state, detail = queue_module.probe_redis_connection("redis.internal:6379")

    assert probe_state == queue_module.RedisConnectionProbe.FATAL
    assert "bad password" in detail


@patch.object(queue_module, "create_redis_connection")
def test_probe_redis_connection_passes_explicit_redis_password(
    mock_create_redis_connection: Mock,
) -> None:
    """Probes should forward explicit Redis credentials."""
    connection = Mock()
    mock_create_redis_connection.return_value = connection

    probe_state, detail = queue_module.probe_redis_connection(
        "redis.internal:6379",
        redis_password="shared-secret",
    )

    assert probe_state == queue_module.RedisConnectionProbe.READY
    assert detail is None
    mock_create_redis_connection.assert_called_once_with(
        "redis.internal:6379",
        socket_connect_timeout=2,
        redis_password="shared-secret",
    )


@patch.object(queue_module.rq, "Queue")
@patch.object(queue_module, "create_redis_connection")
def test_initialize_queue_passes_explicit_redis_password(
    mock_create_redis_connection: Mock,
    mock_queue_cls: Mock,
) -> None:
    """Queue initialization should forward explicit Redis credentials."""
    connection = Mock()
    queue = Mock()
    mock_create_redis_connection.return_value = connection
    mock_queue_cls.return_value = queue

    result = queue_module.initialize_queue(
        "redis.internal:6379",
        "test-exp",
        redis_password="shared-secret",
    )

    assert result is queue
    mock_create_redis_connection.assert_called_once_with(
        "redis.internal:6379",
        redis_password="shared-secret",
    )


@patch.object(queue_module, "time")
@patch.object(queue_module, "probe_redis_connection")
def test_wait_for_redis_connection_retries_until_ready(
    mock_probe_redis_connection: Mock,
    mock_time,
) -> None:
    """Retryable Redis startup failures should be polled until the server is ready."""
    mock_time.monotonic.side_effect = [0.0, 0.0, 0.5]
    mock_probe_redis_connection.side_effect = [
        (queue_module.RedisConnectionProbe.RETRYABLE, "connection reset"),
        (queue_module.RedisConnectionProbe.READY, None),
    ]

    queue_module.wait_for_redis_connection(
        "redis.internal:6379",
        redis_password="shared-secret",
        timeout_sec=30,
        poll_interval_sec=0.5,
        probe_timeout_sec=1,
    )

    assert mock_probe_redis_connection.call_count == 2
    mock_probe_redis_connection.assert_any_call(
        "redis.internal:6379",
        timeout=1,
        redis_password="shared-secret",
    )
    mock_time.sleep.assert_called_once_with(0.5)


@patch.object(queue_module, "probe_redis_connection")
def test_wait_for_redis_connection_raises_on_fatal_probe(
    mock_probe_redis_connection: Mock,
) -> None:
    """Authentication and other fatal probe failures should not be retried."""
    mock_probe_redis_connection.return_value = (
        queue_module.RedisConnectionProbe.FATAL,
        "bad password",
    )

    with pytest.raises(RuntimeError, match="bad password"):
        queue_module.wait_for_redis_connection(
            "redis.internal:6379",
            redis_password="shared-secret",
            timeout_sec=30,
        )


@patch.object(queue_module.redis, "Redis")
def test_create_redis_connection_sets_socket_timeout_for_password_probe(
    mock_redis_cls: Mock,
) -> None:
    """Redis probes should bound read waits as well as connect waits."""
    connection = Mock()
    mock_redis_cls.return_value = connection
    queue_module._auth_required = None

    queue_module.create_redis_connection(
        "redis.internal:6379",
        socket_connect_timeout=7,
        redis_password="shared-secret",
    )

    mock_redis_cls.assert_called_once_with(
        host="redis.internal",
        port=6379,
        password="shared-secret",
        socket_connect_timeout=7,
        socket_timeout=7,
    )
    connection.ping.assert_called_once_with()


@patch.object(queue_module.redis, "Redis")
def test_create_redis_connection_sets_socket_timeout_for_cached_no_auth(
    mock_redis_cls: Mock,
) -> None:
    """Cached no-auth reconnects should also bound read waits."""
    connection = Mock()
    mock_redis_cls.return_value = connection
    queue_module._auth_required = False

    queue_module.create_redis_connection(
        "redis.internal:6379",
        socket_connect_timeout=4,
    )

    mock_redis_cls.assert_called_once_with(
        host="redis.internal",
        port=6379,
        socket_connect_timeout=4,
        socket_timeout=4,
    )
    connection.ping.assert_called_once_with()


def test_default_evaluator_routing_model_is_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing routing model should default to shared mode."""
    monkeypatch.delenv(queue_module.EVALUATOR_ROUTING_MODEL_ENV, raising=False)

    assert (
        queue_module.get_evaluator_routing_model() == queue_module.ROUTING_MODEL_SHARED
    )


def test_invalid_evaluator_routing_model_falls_back_to_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid routing model should fall back to shared."""
    monkeypatch.setenv(queue_module.EVALUATOR_ROUTING_MODEL_ENV, "unsupported")

    assert (
        queue_module.get_evaluator_routing_model() == queue_module.ROUTING_MODEL_SHARED
    )


def test_resolve_evaluator_local_queue_names() -> None:
    """Evaluator-local queue names should include experiment and evaluator ids."""
    build_queue, verify_queue = queue_module.resolve_evaluator_local_queue_names(
        "exp-1",
        "eval-2",
    )

    assert build_queue == "crsbench_exp-1_eval-2_build"
    assert verify_queue == "crsbench_exp-1_eval-2_verify"


def test_resolve_evaluator_local_queue_names_rejects_invalid_components() -> None:
    """Evaluator-local queue names should reject invalid components."""
    with pytest.raises(ValueError, match="Invalid name for queue component"):
        queue_module.resolve_evaluator_local_queue_names("exp-1", "eval 2")
