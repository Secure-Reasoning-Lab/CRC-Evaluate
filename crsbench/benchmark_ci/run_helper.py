"""Backward compatibility shim for run_helper.

This module has been moved to crsbench.utils.run_helper.
All imports are re-exported from there for backward compatibility.
"""

# Re-export everything from the new location
from crsbench.utils.run_helper import (
    # Exit codes and exceptions
    TestExitCode,
    TestExecutionError,
    DockerExecutionError,
    TestFailedError,
    FatalTestError,
    # Result classes
    CommandResult,
    TestResult,
    BuildResult,
    PatchResult,
    # Configuration
    get_oss_fuzz_root,
    get_benchmarks_root,
    get_benchmark_dir,
    # Utilities
    strip_ansi,
    shorten_logs,
    get_workdir_from_dockerfile,
    detect_language,
    get_project_config,
    # Command execution
    run_cmd,
    run_cmd_with_logging,
    run_helper,
    # Exit code handling
    is_docker_execution_error,
    classify_exit_code,
    handle_test_exit_code,
    # Source code management
    get_project_source_dir,
    prepare_benchmark_for_oss_fuzz,
    # Build functions
    build_benchmark,
    build_benchmark_with_logging,
    check_build,
    # Test execution
    run_test_sh,
    # POV reproduction
    reproduce_pov,
    # Patch management
    apply_patch,
    revert_patch,
    verify_bad_patch,
    # Container commands
    run_command_in_container,
    # Benchmark info
    get_benchmark_info,
)

# Legacy aliases for backward compatibility with benchmark_ci.utils
# These were previously defined in benchmark_ci.utils but are now in run_helper
__all__ = [
    # Exit codes and exceptions
    "TestExitCode",
    "TestExecutionError",
    "DockerExecutionError",
    "TestFailedError",
    "FatalTestError",
    # Result classes
    "CommandResult",
    "TestResult",
    "BuildResult",
    "PatchResult",
    # Configuration
    "get_oss_fuzz_root",
    "get_benchmarks_root",
    "get_benchmark_dir",
    # Utilities
    "strip_ansi",
    "shorten_logs",
    "get_workdir_from_dockerfile",
    "detect_language",
    "get_project_config",
    # Command execution
    "run_cmd",
    "run_cmd_with_logging",
    "run_helper",
    # Exit code handling
    "is_docker_execution_error",
    "classify_exit_code",
    "handle_test_exit_code",
    # Source code management
    "get_project_source_dir",
    "prepare_benchmark_for_oss_fuzz",
    # Build functions
    "build_benchmark",
    "build_benchmark_with_logging",
    "check_build",
    # Test execution
    "run_test_sh",
    # POV reproduction
    "reproduce_pov",
    # Patch management
    "apply_patch",
    "revert_patch",
    "verify_bad_patch",
    # Container commands
    "run_command_in_container",
    # Benchmark info
    "get_benchmark_info",
]
