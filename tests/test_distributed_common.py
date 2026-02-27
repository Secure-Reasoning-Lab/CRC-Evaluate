"""Tests for shared distributed runtime helpers."""

from crsbench.distributed.common import normalize_redis_host


def test_normalize_redis_host_string_values() -> None:
    """String hosts are trimmed and sentinel values map to local mode."""
    assert normalize_redis_host(" localhost ") == "localhost"
    assert normalize_redis_host("none") is None
    assert normalize_redis_host(" NONE ") is None
    assert normalize_redis_host("") is None
    assert normalize_redis_host("   ") is None


def test_normalize_redis_host_non_string_values() -> None:
    """Falsy non-strings map to local mode, truthy values stringify."""
    false_value = False
    assert normalize_redis_host(None) is None
    assert normalize_redis_host(false_value) is None
    assert normalize_redis_host(0) is None
    assert normalize_redis_host(6379) == "6379"
