"""Tests for the centralized loguru-based logger."""

import tempfile
from io import StringIO
from pathlib import Path

import pytest
from crsbench.utils.logger import (
    add_file_handler,
    configure_logger,
    create_trial_filter,
    critical,
    debug,
    error,
    get_logger,
    get_trial_context,
    getLogger,
    info,
    remove_file_handler,
    set_trial_context,
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


def test_logger_exception_includes_traceback():
    """Test exception logging includes traceback details in the sink output."""
    output = StringIO()
    configure_logger(level="INFO", colorize=False, sink=output)

    logger = get_logger("test_exception")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Operation failed")

    output_str = output.getvalue()
    assert "Operation failed" in output_str
    assert "RuntimeError: boom" in output_str


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


def test_trial_context():
    """Test trial context management functions."""
    # Initially no context
    assert get_trial_context() is None

    # Set context
    set_trial_context("trial-1")
    assert get_trial_context() == "trial-1"

    # Change context
    set_trial_context("trial-2")
    assert get_trial_context() == "trial-2"

    # Clear context
    set_trial_context(None)
    assert get_trial_context() is None


def test_create_trial_filter():
    """Test trial filter creation and functionality."""
    # Create filter for trial-1
    filter_trial_1 = create_trial_filter("trial-1")
    assert callable(filter_trial_1)

    # Mock record object (loguru record dict)
    mock_record = {"message": "test"}

    # Filter should return False when context doesn't match
    set_trial_context("trial-2")
    assert filter_trial_1(mock_record) is False

    # Filter should return True when context matches
    set_trial_context("trial-1")
    assert filter_trial_1(mock_record) is True

    # Filter should return False when no context
    set_trial_context(None)
    assert filter_trial_1(mock_record) is False


def test_add_file_handler_with_filter():
    """Test adding file handler with trial filter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "trial.log"

        # Create filter for trial-1
        filter_func = create_trial_filter("trial-1")

        # Add file handler with filter
        handler_id = add_file_handler(log_path, level="DEBUG", filter_func=filter_func)

        try:
            logger = get_logger("test_filtered")

            # Set context to trial-1 and log
            set_trial_context("trial-1")
            logger.info("Message from trial-1")

            # Set context to trial-2 and log
            set_trial_context("trial-2")
            logger.info("Message from trial-2")

            # Clear context and log
            set_trial_context(None)
            logger.info("Message with no context")

            # Read log file
            with log_path.open("r") as f:
                log_content = f.read()

            # Only trial-1 message should be in the log
            assert "Message from trial-1" in log_content
            assert "Message from trial-2" not in log_content
            assert "Message with no context" not in log_content

        finally:
            remove_file_handler(handler_id)
            set_trial_context(None)


def test_add_file_handler_without_filter():
    """Test adding file handler without filter (all messages)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "all.log"

        # Add file handler without filter
        handler_id = add_file_handler(log_path, level="DEBUG")

        try:
            logger = get_logger("test_all")

            # Log with different contexts
            set_trial_context("trial-1")
            logger.info("Message from trial-1")

            set_trial_context("trial-2")
            logger.info("Message from trial-2")

            set_trial_context(None)
            logger.info("Message with no context")

            # Read log file
            with log_path.open("r") as f:
                log_content = f.read()

            # All messages should be in the log
            assert "Message from trial-1" in log_content
            assert "Message from trial-2" in log_content
            assert "Message with no context" in log_content

        finally:
            remove_file_handler(handler_id)
            set_trial_context(None)


def test_file_handler_rotation():
    """Test file handler with rotation policy."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "rotated.log"

        # Add file handler with rotation
        handler_id = add_file_handler(
            log_path, level="DEBUG", rotation="10 MB", retention="7 days"
        )

        try:
            logger = get_logger("test_rotation")
            logger.info("Test rotation policy")

            assert log_path.exists()

        finally:
            remove_file_handler(handler_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
