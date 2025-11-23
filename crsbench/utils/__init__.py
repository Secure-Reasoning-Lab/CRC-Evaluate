"""Shared utilities for CRSBench modules."""

from crsbench.utils.logger import (
    get_logger,
    configure_logger,
    getLogger,
    logger,
    debug,
    info,
    success,
    warning,
    error,
    critical,
    # Logging utility functions
    log_section,
    log_summary,
    log_results,
    log_list,
    log_progress,
    log_table,
    log_key_value,
    log_file_info,
    log_error_detail,
)

from crsbench.utils.repo_manager import (
    ensure_project_repository,
    find_or_clone_project,
    get_repo_info_from_benchmark,
    derive_repo_name_from_url,
    clone_repository,
)

__all__ = [
    # Core logging functions
    "get_logger",
    "configure_logger",
    "getLogger",
    "logger",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
    # Logging utility functions
    "log_section",
    "log_summary",
    "log_results",
    "log_list",
    "log_progress",
    "log_table",
    "log_key_value",
    "log_file_info",
    "log_error_detail",
    # Repository management functions
    "ensure_project_repository",
    "find_or_clone_project",
    "get_repo_info_from_benchmark",
    "derive_repo_name_from_url",
    "clone_repository",
]