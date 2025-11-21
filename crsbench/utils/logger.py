"""Centralized logging utility using loguru with colored output support.

This module provides a standardized logging interface for all CRSBench modules.
It uses loguru for structured logging with automatic colored output based on log levels.

Usage:
    from crsbench.utils.logger import get_logger

    logger = get_logger(__name__)
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")

Configuration:
    - Log level can be set via LOG_LEVEL environment variable (default: INFO)
    - Colors are automatically disabled for non-TTY output (e.g., file redirection)
    - Format includes timestamp, level, module name, and message
"""

import sys
import os
from typing import Optional
from loguru import logger as _loguru_logger


# Remove default handler to prevent duplicate logs
_loguru_logger.remove()

# Determine log level from environment variable or default to INFO
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

def _format_module_path(record):
    """Format module path to show main category only.

    Converts 'crsbench.distributed.worker' to '[distributed]'
    Converts 'crsbench.evaluation.runner' to '[evaluation]'
    Converts '__main__' to '[cli]'
    """
    name = record.get("name", "")

    # Handle __main__ specially - use generic CLI category
    if name == "__main__":
        return "[cli]"

    # Remove 'crsbench.' prefix if present
    if name.startswith("crsbench."):
        name = name[9:]  # len("crsbench.") = 9

    # Extract only the first component (main category)
    parts = name.split(".")
    if parts and parts[0]:
        category = parts[0]
    else:
        category = "root"

    return f"[{category}]"


# Color scheme for different log levels with enhanced module display
_COLOR_SCHEME = {
    "TRACE": "<dim><cyan>{time:YYYY-MM-DD HH:mm:ss}</cyan></dim> | <dim><cyan>{level: <8}</cyan></dim> | <dim><magenta>{extra[module_path]: <15}</magenta></dim> | <dim>{message}</dim>",
    "DEBUG": "<cyan>{time:YYYY-MM-DD HH:mm:ss}</cyan> | <bold><blue>{level: <8}</blue></bold> | <magenta>{extra[module_path]: <15}</magenta> | <blue>{message}</blue>",
    "INFO": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <bold><white>{level: <8}</white></bold> | <cyan>{extra[module_path]: <15}</cyan> | <white>{message}</white>",
    "SUCCESS": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <bold><green>{level: <8}</green></bold> | <cyan>{extra[module_path]: <15}</cyan> | <green>{message}</green>",
    "WARNING": "<yellow>{time:YYYY-MM-DD HH:mm:ss}</yellow> | <bold><yellow>{level: <8}</yellow></bold> | <yellow>{extra[module_path]: <15}</yellow> | <yellow>{message}</yellow>",
    "ERROR": "<red>{time:YYYY-MM-DD HH:mm:ss}</red> | <bold><red>{level: <8}</red></bold> | <red>{extra[module_path]: <15}</red> | <red>{message}</red>",
    "CRITICAL": "<red><bold>{time:YYYY-MM-DD HH:mm:ss}</bold></red> | <red><bold>{level: <8}</bold></red> | <red><bold>{extra[module_path]: <15}</bold></red> | <red><bold>{message}</bold></red>",
}

# Format for non-TTY output (no colors)
_PLAIN_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module_path]: <15} | {message}"


def _custom_formatter(record):
    """Custom formatter that adds module_path to record."""
    # Add formatted module path to the record
    record["extra"]["module_path"] = _format_module_path(record)

    # Get the color scheme for this log level
    format_string = _COLOR_SCHEME.get(record["level"].name, _PLAIN_FORMAT)
    return format_string + "\n"

# Auto-detect TTY and configure format accordingly
_is_tty = sys.stdout.isatty()

# Add stdout handler with appropriate format
if _is_tty:
    # Use colored format for TTY
    _loguru_logger.add(
        sys.stdout,
        format=_custom_formatter,
        level=_log_level,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )
else:
    # Use plain format for non-TTY (e.g., file redirection, CI logs)
    def _plain_formatter(record):
        record["extra"]["module_path"] = _format_module_path(record)
        return _PLAIN_FORMAT + "\n"

    _loguru_logger.add(
        sys.stdout,
        format=_plain_formatter,
        level=_log_level,
        colorize=False,
        backtrace=True,
        diagnose=True,
    )


def get_logger(name: Optional[str] = None):
    """Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__ of the calling module).
              If None, returns the root logger.

    Returns:
        A loguru logger instance bound to the specified name.

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("Application started")
    """
    if name:
        return _loguru_logger.bind(name=name)
    return _loguru_logger


def configure_logger(
    level: Optional[str] = None,
    format: Optional[str] = None,
    colorize: Optional[bool] = None,
    sink=sys.stdout,
):
    """Reconfigure the global logger settings.

    Args:
        level: Log level (TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL)
        format: Custom format string (loguru format)
        colorize: Force enable/disable colors (None = auto-detect TTY)
        sink: Output destination (default: sys.stdout)

    Example:
        >>> configure_logger(level="DEBUG", colorize=True)
    """
    _loguru_logger.remove()

    final_level = level or _log_level
    final_colorize = colorize if colorize is not None else _is_tty

    if format:
        # Use custom format
        _loguru_logger.add(
            sink,
            format=format,
            level=final_level,
            colorize=final_colorize,
            backtrace=True,
            diagnose=True,
        )
    elif final_colorize:
        # Use colored format with custom formatter
        _loguru_logger.add(
            sink,
            format=_custom_formatter,
            level=final_level,
            colorize=True,
            backtrace=True,
            diagnose=True,
        )
    else:
        # Use plain format
        def _plain_formatter(record):
            record["extra"]["module_path"] = _format_module_path(record)
            return _PLAIN_FORMAT + "\n"

        _loguru_logger.add(
            sink,
            format=_plain_formatter,
            level=final_level,
            colorize=False,
            backtrace=True,
            diagnose=True,
        )


# Provide shortcuts to common logging functions
def debug(message: str, **kwargs):
    """Log a debug message."""
    _loguru_logger.debug(message, **kwargs)


def info(message: str, **kwargs):
    """Log an info message."""
    _loguru_logger.info(message, **kwargs)


def success(message: str, **kwargs):
    """Log a success message (loguru-specific level)."""
    _loguru_logger.success(message, **kwargs)


def warning(message: str, **kwargs):
    """Log a warning message."""
    _loguru_logger.warning(message, **kwargs)


def error(message: str, **kwargs):
    """Log an error message."""
    _loguru_logger.error(message, **kwargs)


def critical(message: str, **kwargs):
    """Log a critical message."""
    _loguru_logger.critical(message, **kwargs)


# Backwards compatibility with standard logging module
class LoggerAdapter:
    """Adapter to make loguru logger compatible with standard logging interface.

    This allows gradual migration from standard logging to loguru.
    """

    def __init__(self, name: str):
        self._logger = get_logger(name)

    def debug(self, msg, *args, **kwargs):
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._logger.exception(msg, *args, **kwargs)


def getLogger(name: Optional[str] = None) -> LoggerAdapter:
    """Get a logger adapter compatible with standard logging.getLogger() interface.

    This function provides backwards compatibility with code using standard logging.

    Args:
        name: Logger name (typically __name__)

    Returns:
        LoggerAdapter instance that wraps loguru logger

    Example:
        >>> logger = getLogger(__name__)  # Drop-in replacement for logging.getLogger()
        >>> logger.info("Message")
    """
    return LoggerAdapter(name or "root")


# Export the main logger for direct use
logger = _loguru_logger
