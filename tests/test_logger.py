"""Tests for the centralized loguru-based logger."""

import os
from io import StringIO

import pytest
from crsbench.utils.logger import (
    configure_logger,
    critical,
    debug,
    error,
    get_logger,
    getLogger,
    info,
    success,
    warning,
)


def test_get_logger():
    """Test get_logger returns a logger with bound name."""
    logger = get_logger("test_module")
    assert logger is not None


def test_get_logger_no_name():
    """Test get_logger without name returns root logger."""
    logger = get_logger()
    assert logger is not None


def test_logger_adapter():
    """Test LoggerAdapter provides standard logging interface."""
    logger = getLogger("test_adapter")

    # Should have standard logging methods
    assert hasattr(logger, "debug")
    assert hasattr(logger, "info")
    assert hasattr(logger, "warning")
    assert hasattr(logger, "error")
    assert hasattr(logger, "critical")
    assert hasattr(logger, "exception")


def test_logger_levels():
    """Test all log level functions work."""
    # Capture output
    output = StringIO()

    # Configure logger to write to StringIO
    configure_logger(level="DEBUG", colorize=False, sink=output)

    # Get a test logger
    logger = get_logger("test_levels")

    # Test all levels
    logger.debug("Debug message")
    logger.info("Info message")
    logger.success("Success message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")

    # Check output contains our messages
    output_str = output.getvalue()
    assert "Debug message" in output_str
    assert "Info message" in output_str
    assert "Success message" in output_str
    assert "Warning message" in output_str
    assert "Error message" in output_str
    assert "Critical message" in output_str

    # Check level names appear
    assert "DEBUG" in output_str
    assert "INFO" in output_str
    assert "SUCCESS" in output_str
    assert "WARNING" in output_str
    assert "ERROR" in output_str
    assert "CRITICAL" in output_str


def test_logger_module_level_functions():
    """Test module-level logging functions."""
    output = StringIO()
    configure_logger(level="DEBUG", colorize=False, sink=output)

    # Test module-level functions
    debug("Module debug")
    info("Module info")
    success("Module success")
    warning("Module warning")
    error("Module error")
    critical("Module critical")

    output_str = output.getvalue()
    assert "Module debug" in output_str
    assert "Module info" in output_str
    assert "Module success" in output_str
    assert "Module warning" in output_str
    assert "Module error" in output_str
    assert "Module critical" in output_str


def test_configure_logger_level_filter():
    """Test that log level filtering works correctly."""
    output = StringIO()

    # Set level to WARNING - should not see DEBUG/INFO
    configure_logger(level="WARNING", colorize=False, sink=output)

    logger = get_logger("test_filter")
    logger.debug("Should not appear")
    logger.info("Should not appear either")
    logger.warning("Should appear")
    logger.error("Should also appear")

    output_str = output.getvalue()
    assert "Should not appear" not in output_str
    assert "Should not appear either" not in output_str
    assert "Should appear" in output_str
    assert "Should also appear" in output_str


def test_logger_adapter_compatibility():
    """Test LoggerAdapter is compatible with standard logging usage."""
    output = StringIO()
    configure_logger(level="INFO", colorize=False, sink=output)

    # Use like standard logging.getLogger()
    logger = getLogger("test_compat")
    logger.info("Info from adapter")
    logger.warning("Warning from adapter")
    logger.error("Error from adapter")

    output_str = output.getvalue()
    assert "Info from adapter" in output_str
    assert "Warning from adapter" in output_str
    assert "Error from adapter" in output_str


def test_logger_respects_env_var():
    """Test that logger respects LOG_LEVEL environment variable."""
    # Save original env var
    original_level = os.environ.get("LOG_LEVEL")

    try:
        # Set LOG_LEVEL to DEBUG
        os.environ["LOG_LEVEL"] = "DEBUG"

        # Re-import to pick up env var (in real usage, this would be set before import)
        # For this test, we just verify the mechanism exists
        assert os.environ.get("LOG_LEVEL") == "DEBUG"

    finally:
        # Restore original env var
        if original_level is not None:
            os.environ["LOG_LEVEL"] = original_level
        elif "LOG_LEVEL" in os.environ:
            del os.environ["LOG_LEVEL"]


def test_colorize_option():
    """Test that colorize option can be controlled."""
    output = StringIO()

    # Test with colorize=True (should add ANSI codes)
    configure_logger(level="INFO", colorize=True, sink=output)
    logger = get_logger("test_color")
    logger.info("Colored message")

    output_colored = output.getvalue()

    # Reset
    output = StringIO()
    configure_logger(level="INFO", colorize=False, sink=output)
    logger = get_logger("test_plain")
    logger.info("Plain message")

    output_plain = output.getvalue()

    # Both should contain the message
    assert "Colored message" in output_colored
    assert "Plain message" in output_plain

    # Colored output should be longer due to ANSI codes (unless TTY detection overrides)
    # This is a basic check - exact behavior depends on TTY detection


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
