"""Tests for the validation module."""

import pytest
from crsbench.validation import validate_benchmark_from_string


def test_validate_correct_meta_yaml():
    """Test validation with a correct meta.yaml configuration."""

    valid_yaml = """
patch_exclude_list:
  - "build.sh"
  - "test/**"
  - "**/*test*.c"

full_mode:
  base_commit: "abc123def456"

harness_files:
  - name: "fuzz_parser"
    path: "$REPO/test/fuzz_parser.c"
    povs:
      - name: "buffer_overflow_main"
        sanitizer: "address"
        error_token: "AddressSanitizer: heap-buffer-overflow"
        requires_clean_build: false
"""

    result = validate_benchmark_from_string(valid_yaml)

    # Should be valid
    assert result.is_valid is True
    assert result.error_count == 0

    # Check metadata
    assert result.metadata["total_harnesses"] == 1
    assert result.metadata["total_povs"] == 1
    assert result.metadata["has_full_mode"] is True
    assert result.metadata["has_delta_mode"] is False


def test_validate_incorrect_meta_yaml():
    """Test validation with an incorrect meta.yaml configuration."""

    invalid_yaml = """
patch_exclude_list:
  - "build.sh"
  - "test/**"

full_mode:
  base_commit: "abc123def456"

# Missing required harness_files field
"""

    result = validate_benchmark_from_string(invalid_yaml)

    # Should be invalid
    assert result.is_valid is False
    assert result.error_count > 0

    # Should have errors about missing harness_files
    error_messages = [error.message for error in result.errors]
    assert any("harness_files" in msg.lower() for msg in error_messages)