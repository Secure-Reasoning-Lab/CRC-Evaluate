"""Shared utilities for CRSBench modules."""

from crsbench.utils.logger import (
    configure_logger,
    critical,
    debug,
    error,
    get_logger,
    getLogger,
    info,
    log_error_detail,
    log_file_info,
    log_key_value,
    log_list,
    log_progress,
    log_results,
    # Logging utility functions
    log_section,
    log_summary,
    log_table,
    logger,
    success,
    warning,
)
from crsbench.utils.repo_manager import (
    clone_repository,
    derive_repo_name_from_url,
    ensure_project_repository,
    find_or_clone_project,
    get_repo_info_from_benchmark,
    run_git,
    set_gitcache,
)
from crsbench.utils.run_helper import (
    BuildResult,
    # Result classes
    CommandResult,
    DockerExecutionError,
    FatalTestError,
    PatchResult,
    TestExecutionError,
    # Exit codes and exceptions
    TestExitCode,
    TestFailedError,
    TestResult,
    # Patch management
    apply_patch,
    # Build functions
    build_benchmark,
    build_benchmark_with_logging,
    check_build,
    classify_exit_code,
    detect_language,
    get_benchmark_dir,
    # Benchmark info
    get_benchmark_info,
    get_benchmarks_root,
    # Configuration
    get_oss_fuzz_root,
    get_project_config,
    # Source code management
    get_project_source_dir,
    get_workdir_from_dockerfile,
    handle_test_exit_code,
    # Exit code handling
    is_docker_execution_error,
    prepare_benchmark_for_oss_fuzz,
    # POV reproduction
    reproduce_pov,
    revert_patch,
    # Command execution
    run_cmd,
    run_cmd_with_logging,
    # Container commands
    run_command_in_container,
    run_helper,
    # Test execution
    run_test_sh,
    run_with_rolling_output,
    shorten_logs,
    # Utilities
    strip_ansi,
    verify_bad_patch,
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
    "set_gitcache",
    "run_git",
    # Run helper - Exit codes and exceptions
    "TestExitCode",
    "TestExecutionError",
    "DockerExecutionError",
    "TestFailedError",
    "FatalTestError",
    # Run helper - Result classes
    "CommandResult",
    "TestResult",
    "BuildResult",
    "PatchResult",
    # Run helper - Configuration
    "get_oss_fuzz_root",
    "get_benchmarks_root",
    "get_benchmark_dir",
    # Run helper - Utilities
    "strip_ansi",
    "shorten_logs",
    "get_workdir_from_dockerfile",
    "detect_language",
    "get_project_config",
    # Run helper - Command execution
    "run_cmd",
    "run_cmd_with_logging",
    "run_helper",
    "run_with_rolling_output",
    # Run helper - Exit code handling
    "is_docker_execution_error",
    "classify_exit_code",
    "handle_test_exit_code",
    # Run helper - Source code management
    "get_project_source_dir",
    "prepare_benchmark_for_oss_fuzz",
    # Run helper - Build functions
    "build_benchmark",
    "build_benchmark_with_logging",
    "check_build",
    # Run helper - Test execution
    "run_test_sh",
    # Run helper - POV reproduction
    "reproduce_pov",
    # Run helper - Patch management
    "apply_patch",
    "revert_patch",
    "verify_bad_patch",
    # Run helper - Container commands
    "run_command_in_container",
    # Run helper - Benchmark info
    "get_benchmark_info",
]
