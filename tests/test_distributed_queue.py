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
