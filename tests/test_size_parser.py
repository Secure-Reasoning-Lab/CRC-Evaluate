"""Tests for size parser utility."""

import pytest
from crsbench.utils.size_parser import parse_size_to_bytes


def test_parse_size_bytes():
    """Test parsing bytes."""
    assert parse_size_to_bytes("100B") == 100
    assert parse_size_to_bytes("1B") == 1


def test_parse_size_kilobytes():
    """Test parsing kilobytes."""
    assert parse_size_to_bytes("1KB") == 1024
    assert parse_size_to_bytes("10KB") == 10 * 1024
    assert parse_size_to_bytes("1K") == 1024


def test_parse_size_megabytes():
    """Test parsing megabytes."""
    assert parse_size_to_bytes("1MB") == 1024**2
    assert parse_size_to_bytes("100MB") == 100 * 1024**2
    assert parse_size_to_bytes("1M") == 1024**2


def test_parse_size_gigabytes():
    """Test parsing gigabytes."""
    assert parse_size_to_bytes("1GB") == 1024**3
    assert parse_size_to_bytes("200GB") == 200 * 1024**3
    assert parse_size_to_bytes("1G") == 1024**3


def test_parse_size_terabytes():
    """Test parsing terabytes."""
    assert parse_size_to_bytes("1TB") == 1024**4
    assert parse_size_to_bytes("2TB") == 2 * 1024**4
    assert parse_size_to_bytes("1T") == 1024**4


def test_parse_size_with_decimals():
    """Test parsing sizes with decimal values."""
    assert parse_size_to_bytes("1.5GB") == int(1.5 * 1024**3)
    assert parse_size_to_bytes("0.5MB") == int(0.5 * 1024**2)


def test_parse_size_with_whitespace():
    """Test parsing sizes with whitespace."""
    assert parse_size_to_bytes("  200GB  ") == 200 * 1024**3
    assert parse_size_to_bytes("100 MB") == 100 * 1024**2


def test_parse_size_case_insensitive():
    """Test that parsing is case insensitive."""
    assert parse_size_to_bytes("200gb") == 200 * 1024**3
    assert parse_size_to_bytes("100mb") == 100 * 1024**2
    assert parse_size_to_bytes("1Gb") == 1024**3


def test_parse_size_invalid_format():
    """Test that invalid formats raise ValueError."""
    with pytest.raises(ValueError, match="Invalid size format"):
        parse_size_to_bytes("invalid")

    with pytest.raises(ValueError, match="Invalid size format"):
        parse_size_to_bytes("100")

    with pytest.raises(ValueError, match="Invalid size format"):
        parse_size_to_bytes("GB100")


def test_parse_size_unknown_unit():
    """Test that unknown units raise ValueError."""
    with pytest.raises(ValueError, match="Unknown unit"):
        parse_size_to_bytes("100XB")

    with pytest.raises(ValueError, match="Unknown unit"):
        parse_size_to_bytes("100PB")
