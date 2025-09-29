"""Benchmark format validation module.

This module provides robust validation of benchmark configurations with minimal
side effects, suitable for use as tool calls by LLM agents.
"""

from crsbench.validation.format_validator import validate_benchmark, validate_benchmark_from_string
from crsbench.validation.errors import ValidationResult, ValidationError, ValidationWarning

__all__ = ['validate_benchmark', 'validate_benchmark_from_string', 'ValidationResult', 'ValidationError', 'ValidationWarning']