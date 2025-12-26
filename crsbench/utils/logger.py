"""Centralized logging utility using loguru with colored output support.

This module provides a standardized logging interface for all CRSBench modules.
It uses loguru for structured logging with automatic colored output based on log levels.

Basic Usage:
    from crsbench.utils.logger import get_logger

    logger = get_logger(__name__)
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")

Utility Functions Usage:
    from crsbench.utils import log_section, log_summary, log_results

    # Section headers
    log_section("Build Analysis Results")

    # Summary with statistics
    log_summary("Build Results", {
        "total": 10,
        "success": 7,
        "failed": 3
    })

    # Success/failure lists
    log_results(
        success_items=["project-a", "project-b"],
        failed_items=[
            ("project-c", "Build error"),
            ("project-d", "Test failed")
        ]
    )

    # Other utilities
    from crsbench.utils import log_list, log_progress, log_table, log_key_value, log_file_info

    log_list(["item1", "item2"], title="My List", symbol="✓")
    log_progress(7, 10, "Projects processed")
    log_table(["Name", "Status"], [["proj-a", "OK"], ["proj-b", "FAIL"]])
    log_key_value({"Benchmark": "curl", "Status": "Success"})
    log_file_info("/path/to/file.txt", "Report saved")

Configuration:
    - Log level can be set via LOG_LEVEL environment variable (default: INFO)
    - Colors are automatically disabled for non-TTY output (e.g., file redirection)
    - Format includes timestamp, level, module name, and message
"""

import os
import sys
from pathlib import Path
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
_PLAIN_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module_path]: <15} | {message}"
)


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


# ============================================================================
# Logging Utility Functions
# ============================================================================
# Common logging patterns for consistent output formatting


def log_section(title: str, width: int = 70, char: str = "=", level: str = "info"):
    """Log a section header with separator lines.

    Args:
        title: Section title
        width: Width of separator line
        char: Character to use for separator
        level: Log level (info, debug, warning, error)

    Example:
        >>> log_section("Build Analysis Results")

        ======================================================================
        Build Analysis Results
        ======================================================================

    """
    log_func = getattr(_loguru_logger, level.lower())
    separator = char * width
    log_func("")  # Empty line before
    log_func(separator)
    log_func(title)
    log_func(separator)
    log_func("")  # Empty line after


def log_summary(
    title: str,
    stats: dict,
    level: str = "info",
    *,
    show_percentage: bool = True,
):
    """Log a summary with statistics.

    Args:
        title: Summary title
        stats: Dictionary of statistics (e.g., {"total": 10, "success": 7, "failed": 3})
        level: Log level
        show_percentage: Show percentage for each stat

    Example:
        >>> log_summary("Build Results", {"total": 10, "success": 7, "failed": 3})
        Build Results Summary
        Total: 10
        Success: 7 (70.0%)
        Failed: 3 (30.0%)
    """
    log_func = getattr(_loguru_logger, level.lower())

    log_func(f"\n{title} Summary")

    total = stats.get("total", sum(stats.values()))

    for key, value in stats.items():
        key_display = key.replace("_", " ").title()
        if show_percentage and key != "total" and total > 0:
            percentage = (value / total) * 100
            log_func(f"{key_display}: {value} ({percentage:.1f}%)")
        else:
            log_func(f"{key_display}: {value}")


def log_results(
    success_items: list,
    failed_items: list,
    success_title: str = "Successful",
    failed_title: str = "Failed",
    success_symbol: str = "✓",
    failed_symbol: str = "✗",
    level: str = "info",
):
    """Log success and failure lists with symbols.

    Args:
        success_items: List of successful items
        failed_items: List of failed items (can be tuples of (item, reason))
        success_title: Title for success section
        failed_title: Title for failed section
        success_symbol: Symbol for success items
        failed_symbol: Symbol for failed items
        level: Log level

    Example:
        >>> log_results(
        ...     success_items=["project-a", "project-b"],
        ...     failed_items=[("project-c", "Build error"), ("project-d", "Test failed")]
        ... )
        Successful (2):
        1. project-a ✓
        2. project-b ✓

        Failed (2):
        1. project-c ✗
           - Build error
        2. project-d ✗
           - Test failed
    """
    log_func = getattr(_loguru_logger, level.lower())

    # Log successful items
    if success_items:
        log_func(f"\n{success_title} ({len(success_items)}):\n")
        for idx, item in enumerate(success_items, 1):
            log_func(f"{idx}. {item} {success_symbol}")

    # Log failed items
    if failed_items:
        log_func(f"\n{failed_title} ({len(failed_items)}):\n")
        for idx, item in enumerate(failed_items, 1):
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                name, reason = item[0], item[1]
                log_func(f"{idx}. {name} {failed_symbol}")
                log_func(f"   - {reason}")
            else:
                log_func(f"{idx}. {item} {failed_symbol}")


def log_list(
    items: list,
    title: Optional[str] = None,
    symbol: Optional[str] = None,
    level: str = "info",
    indent: int = 0,
    *,
    numbered: bool = True,
):
    """Log a formatted list of items.

    Args:
        items: List of items to log
        title: Optional title for the list
        numbered: Use numbered list (1. 2. 3.) instead of bullets
        symbol: Optional symbol to append to each item
        level: Log level
        indent: Indentation level (spaces)

    Example:
        >>> log_list(["item1", "item2", "item3"], title="My List", symbol="✓")
        My List:
        1. item1 ✓
        2. item2 ✓
        3. item3 ✓
    """
    log_func = getattr(_loguru_logger, level.lower())

    if title:
        log_func(f"\n{title}:")

    indent_str = " " * indent

    for idx, item in enumerate(items, 1):
        if numbered:
            prefix = f"{idx}."
        else:
            prefix = "•"

        suffix = f" {symbol}" if symbol else ""
        log_func(f"{indent_str}{prefix} {item}{suffix}")


def log_progress(
    current: int,
    total: int,
    description: str = "Progress",
    level: str = "info",
    *,
    show_percentage: bool = True,
):
    """Log progress information.

    Args:
        current: Current progress count
        total: Total count
        description: Description of what's being tracked
        level: Log level
        show_percentage: Show percentage

    Example:
        >>> log_progress(7, 10, "Projects processed")
        Progress: 7/10 (70.0%)
    """
    log_func = getattr(_loguru_logger, level.lower())

    if show_percentage and total > 0:
        percentage = (current / total) * 100
        log_func(f"{description}: {current}/{total} ({percentage:.1f}%)")
    else:
        log_func(f"{description}: {current}/{total}")


def log_table(
    headers: list,
    rows: list,
    title: Optional[str] = None,
    level: str = "info",
    min_col_width: int = 15,
):
    """Log a simple table.

    Args:
        headers: List of column headers
        rows: List of rows (each row is a list of values)
        title: Optional table title
        level: Log level
        min_col_width: Minimum column width

    Example:
        >>> log_table(
        ...     headers=["Project", "Status", "Time"],
        ...     rows=[
        ...         ["project-a", "Success", "10s"],
        ...         ["project-b", "Failed", "5s"]
        ...     ],
        ...     title="Build Results"
        ... )
        Build Results
        Project         Status          Time
        ─────────────────────────────────────────────────
        project-a       Success         10s
        project-b       Failed          5s
    """
    log_func = getattr(_loguru_logger, level.lower())

    # Calculate column widths
    col_widths = [max(min_col_width, len(str(h))) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(str(cell)))

    # Log title
    if title:
        log_func(f"\n{title}")

    # Log headers
    header_line = "".join(
        str(h).ljust(col_widths[idx] + 2) for idx, h in enumerate(headers)
    )
    log_func(header_line)

    # Log separator
    separator = "─" * (sum(col_widths) + len(headers) * 2)
    log_func(separator)

    # Log rows
    for row in rows:
        row_line = "".join(
            str(cell).ljust(col_widths[idx] + 2) for idx, cell in enumerate(row)
        )
        log_func(row_line)


def log_key_value(
    data: dict, title: Optional[str] = None, level: str = "info", indent: int = 2
):
    """Log key-value pairs in a formatted way.

    Args:
        data: Dictionary of key-value pairs
        title: Optional title
        level: Log level
        indent: Indentation for values

    Example:
        >>> log_key_value(
        ...     {"Benchmark": "curl-delta-01", "Language": "C", "Status": "Success"},
        ...     title="Project Info"
        ... )
        Project Info:
          Benchmark: curl-delta-01
          Language: C
          Status: Success
    """
    log_func = getattr(_loguru_logger, level.lower())

    if title:
        log_func(f"\n{title}:")

    indent_str = " " * indent
    max_key_len = max(len(str(k)) for k in data.keys()) if data else 0

    for key, value in data.items():
        key_str = str(key).ljust(max_key_len)
        log_func(f"{indent_str}{key_str}: {value}")


def log_file_info(
    file_path: str, description: Optional[str] = None, level: str = "info"
):
    """Log file path information with check if file exists.

    Args:
        file_path: Path to file
        description: Optional description
        level: Log level

    Example:
        >>> log_file_info("/path/to/build_analysis.md", "Build analysis saved")
        ✓ Build analysis saved: /path/to/build_analysis.md
    """
    log_func = getattr(_loguru_logger, level.lower())

    exists = Path(file_path).exists()
    symbol = "✓" if exists else "✗"

    if description:
        log_func(f"{symbol} {description}: {file_path}")
    else:
        log_func(f"{symbol} {file_path}")


def log_error_detail(
    error: Exception,
    context: Optional[str] = None,
    max_length: Optional[int] = None,
    level: str = "error",
):
    """Log error with details, handling long error messages gracefully.

    Args:
        error: Exception to log
        context: Optional context description
        max_length: Maximum length for error message (None = no limit)
        level: Log level

    Example:
        >>> try:
        ...     something()
        ... except Exception as e:
        ...     log_error_detail(e, "Failed to process benchmark")
        Failed to process benchmark
        Error type: RuntimeError
        Message: Command 'docker run...' failed with return code 1
                 stdout: ...
                 stderr: ...
    """
    log_func = getattr(_loguru_logger, level.lower())

    # Log context if provided
    if context:
        log_func(context)

    # Log error type
    error_type = type(error).__name__
    log_func(f"Error type: {error_type}")

    # Get error message
    error_msg = str(error)

    # Handle long messages
    if max_length and len(error_msg) > max_length:
        # Truncate and add indicator
        truncated = error_msg[:max_length]
        log_func(f"Message: {truncated}... (truncated, {len(error_msg)} total chars)")
    elif len(error_msg) > 500:
        # Split long messages into multiple lines for readability
        log_func("Message:")
        # Split by newlines if present
        if "\n" in error_msg:
            for line in error_msg.split("\n")[:20]:  # Limit to 20 lines
                log_func(f"  {line}")
            if error_msg.count("\n") > 20:
                log_func(f"  ... ({error_msg.count('\n') - 20} more lines)")
        else:
            # Split long single line into chunks
            chunk_size = 200
            for i in range(0, min(len(error_msg), 2000), chunk_size):
                log_func(f"  {error_msg[i : i + chunk_size]}")
            if len(error_msg) > 2000:
                log_func(f"  ... ({len(error_msg) - 2000} more chars)")
    else:
        # Normal length message
        log_func(f"Message: {error_msg}")
