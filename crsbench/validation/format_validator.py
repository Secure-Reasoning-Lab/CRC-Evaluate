"""Core format validation logic for benchmarks.

This module provides pure functions with minimal side effects for validating
benchmark configurations. Safe for use as tool calls by LLM agents.
"""

import os
import yaml
from typing import Union, Dict, Any
from pathlib import Path
from pydantic import ValidationError as PydanticValidationError

from crsbench.validation.schemas import BenchmarkConfig, ValidationMetadata, HarnessFile, FullMode
from crsbench.validation.errors import (
    ValidationResult, ValidationError, ValidationCodes,
    ValidationSeverity
)


def validate_benchmark(path: Union[str, Path]) -> ValidationResult:
    """
    Validate a benchmark configuration.

    This is a pure function with no side effects that validates the format
    of a benchmark configuration. It only reads files and returns validation
    results without modifying anything.

    Args:
        path: Path to the benchmark directory or meta.yaml file

    Returns:
        ValidationResult: Comprehensive validation results

    Raises:
        ValidationError: Only for unexpected errors during validation process
                        (not for validation failures)
    """
    result = ValidationResult(is_valid=True)

    try:
        # Determine the path to meta.yaml
        meta_yaml_path = _resolve_meta_yaml_path(path)

        # Update metadata with file info
        if meta_yaml_path.exists():
            result.metadata["file_path"] = str(meta_yaml_path)
            result.metadata["file_size"] = meta_yaml_path.stat().st_size
        else:
            result.add_error(
                ValidationCodes.FILE_NOT_FOUND,
                f"meta.yaml not found at expected location: {meta_yaml_path}",
                context={"expected_path": str(meta_yaml_path)}
            )
            return result

        # Validate file is readable
        if not os.access(meta_yaml_path, os.R_OK):
            result.add_error(
                ValidationCodes.FILE_NOT_READABLE,
                f"Cannot read file: {meta_yaml_path}",
                context={"path": str(meta_yaml_path)}
            )
            return result

        # Load and parse YAML
        yaml_content = _load_yaml_file(meta_yaml_path, result)
        if not result.is_valid:
            return result

        # Validate against schema
        config = _validate_schema(yaml_content, result)
        if not result.is_valid:
            return result

        # Perform additional validation checks
        _validate_configuration_logic(config, result)

        # Generate metadata
        _generate_metadata(config, result)

        # Add warnings for common issues
        _check_for_warnings(config, result)

    except Exception as e:
        # Catch unexpected errors during validation process
        raise ValidationError(
            f"Unexpected error during validation: {str(e)}",
            code="VALIDATION_PROCESS_ERROR",
            context={"path": str(path), "error": str(e)}
        )

    return result


def validate_benchmark_from_string(yaml_content: str) -> ValidationResult:
    """
    Validate benchmark configuration from YAML string.

    Pure function that validates YAML content without file system access.

    Args:
        yaml_content: YAML content as string

    Returns:
        ValidationResult: Comprehensive validation results
    """
    result = ValidationResult(is_valid=True)

    try:
        # Parse YAML content
        try:
            data = yaml.safe_load(yaml_content)
            result.metadata["yaml_valid"] = True
        except yaml.YAMLError as e:
            result.add_error(
                ValidationCodes.YAML_SYNTAX_ERROR,
                f"Invalid YAML syntax: {str(e)}",
                context={"yaml_error": str(e)}
            )
            return result

        # Check for empty content
        if data is None:
            result.add_error(
                ValidationCodes.EMPTY_FILE,
                "YAML content is empty"
            )
            return result

        # Validate against schema
        config = _validate_schema(data, result)
        if not result.is_valid:
            return result

        # Perform additional validation checks
        _validate_configuration_logic(config, result)

        # Generate metadata
        _generate_metadata(config, result)

        # Add warnings for common issues
        _check_for_warnings(config, result)

    except Exception as e:
        raise ValidationError(
            f"Unexpected error during validation: {str(e)}",
            code="VALIDATION_PROCESS_ERROR",
            context={"error": str(e)}
        )

    return result


def _resolve_meta_yaml_path(path: Union[str, Path]) -> Path:
    """
    Resolve the path to meta.yaml file.

    Args:
        path: Either path to benchmark directory or meta.yaml file

    Returns:
        Path: Path to meta.yaml file
    """
    path = Path(path)

    if path.is_file() and path.name == "meta.yaml":
        return path
    elif path.is_dir():
        # Look for meta.yaml in .aixcc subdirectory
        aixcc_path = path / ".aixcc" / "meta.yaml"
        if aixcc_path.exists():
            return aixcc_path

        # Look for meta.yaml in root directory
        root_path = path / "meta.yaml"
        if root_path.exists():
            return root_path

        # Return expected path even if it doesn't exist
        return aixcc_path
    else:
        # Assume it's meant to be meta.yaml file
        return path


def _load_yaml_file(file_path: Path, result: ValidationResult) -> Dict[str, Any]:
    """Load and parse YAML file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.strip():
            result.add_error(
                ValidationCodes.EMPTY_FILE,
                "meta.yaml file is empty"
            )
            return {}

        data = yaml.safe_load(content)
        result.metadata["yaml_valid"] = True

        if data is None:
            result.add_error(
                ValidationCodes.EMPTY_FILE,
                "meta.yaml contains no data"
            )
            return {}

        return data

    except yaml.YAMLError as e:
        result.add_error(
            ValidationCodes.YAML_SYNTAX_ERROR,
            f"Invalid YAML syntax: {str(e)}",
            context={"yaml_error": str(e)}
        )
        return {}
    except Exception as e:
        result.add_error(
            ValidationCodes.FILE_NOT_READABLE,
            f"Error reading file: {str(e)}",
            context={"error": str(e)}
        )
        return {}


def _validate_schema(data: Dict[str, Any], result: ValidationResult) -> BenchmarkConfig:
    """Validate data against Pydantic schema."""
    try:
        config = BenchmarkConfig(**data)
        result.metadata["schema_valid"] = True
        return config
    except PydanticValidationError as e:
        result.metadata["schema_valid"] = False
        for error in e.errors():
            field_path = ".".join(str(item) for item in error["loc"])
            result.add_error(
                ValidationCodes.SCHEMA_VALIDATION_ERROR,
                f"Schema validation error in '{field_path}': {error['msg']}",
                field=field_path,
                context={"validation_error": error}
            )
        # Return a minimal config to continue validation
        dummy_harness = HarnessFile(name="dummy", path="$REPO/dummy.c")
        dummy_full_mode = FullMode(base_commit="abc123def456")
        return BenchmarkConfig(harness_files=[dummy_harness], full_mode=dummy_full_mode)
    except Exception as e:
        result.add_error(
            ValidationCodes.SCHEMA_VALIDATION_ERROR,
            f"Schema validation failed: {str(e)}",
            context={"error": str(e)}
        )
        dummy_harness = HarnessFile(name="dummy", path="$REPO/dummy.c")
        dummy_full_mode = FullMode(base_commit="abc123def456")
        return BenchmarkConfig(harness_files=[dummy_harness], full_mode=dummy_full_mode)


def _validate_configuration_logic(config: BenchmarkConfig, result: ValidationResult):
    """Perform additional logical validation beyond schema."""

    # Check for at least one evaluation mode
    if not config.delta_mode and not config.full_mode:
        result.add_error(
            ValidationCodes.NO_EVALUATION_MODE,
            "At least one evaluation mode (delta_mode or full_mode) must be specified"
        )

    # Validate harness files
    if not config.harness_files:
        result.add_error(
            ValidationCodes.NO_HARNESS_FILES,
            "At least one harness file must be specified"
        )

    # Check for harnesses with POVs
    harnesses_with_povs = [h for h in config.harness_files if h.povs]
    if not harnesses_with_povs:
        result.add_warning(
            ValidationCodes.EMPTY_POV_LIST,
            "No harness files have POV configurations"
        )

    # Validate commit hashes in delta mode
    if config.delta_mode:
        if config.delta_mode.base_commit == config.delta_mode.ref_commit:
            result.add_error(
                ValidationCodes.INVALID_COMMIT_HASH,
                "Delta mode base_commit and ref_commit cannot be the same",
                field="delta_mode"
            )


def _generate_metadata(config: BenchmarkConfig, result: ValidationResult):
    """Generate metadata about the configuration."""
    result.metadata.update({
        "total_harnesses": len(config.harness_files),
        "total_povs": sum(len(h.povs or []) for h in config.harness_files),
        "has_delta_mode": config.delta_mode is not None,
        "has_full_mode": config.full_mode is not None,
        "patch_exclude_patterns": len(config.patch_exclude_list or [])
    })


def _check_for_warnings(config: BenchmarkConfig, result: ValidationResult):
    """Add warnings for potential issues."""

    # Warn if no patch exclusion list
    if not config.patch_exclude_list:
        result.add_warning(
            ValidationCodes.NO_PATCH_EXCLUSIONS,
            "No patch exclusion patterns specified. Consider adding common patterns like 'test/**', 'build.sh'"
        )

    # Warn about many harnesses
    if len(config.harness_files) > 20:
        result.add_warning(
            ValidationCodes.MANY_HARNESSES,
            f"Large number of harnesses ({len(config.harness_files)}). Consider grouping related harnesses."
        )

    # Warn about complex path patterns
    complex_patterns = [
        pattern for pattern in (config.patch_exclude_list or [])
        if "**" in pattern and len(pattern.split("**")) > 2
    ]
    if complex_patterns:
        result.add_warning(
            ValidationCodes.COMPLEX_PATHS,
            f"Complex glob patterns detected: {', '.join(complex_patterns[:3])}{'...' if len(complex_patterns) > 3 else ''}. Ensure they work as expected."
        )