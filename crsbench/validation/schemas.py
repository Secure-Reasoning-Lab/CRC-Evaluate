"""Pydantic schemas for benchmark configuration validation."""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, validator
import re


class POV(BaseModel):
    """Proof of Vulnerability configuration."""

    id: str = Field(..., description="POV variant ID (e.g., pov_0, pov_1)")
    sanitizer: str = Field(..., description="Sanitizer type (address, memory, undefined, etc.)")
    error_token: Optional[str] = Field(default=None, description="Expected error pattern from sanitizer (optional)")

    @validator('id')
    def validate_id(cls, v):
        if not v or not v.strip():
            raise ValueError("POV id cannot be empty")
        return v.strip()

    @validator('sanitizer')
    def validate_sanitizer(cls, v):
        valid_sanitizers = {'address', 'memory', 'thread', 'undefined', 'leak'}
        if v not in valid_sanitizers:
            raise ValueError(f"Invalid sanitizer: {v}. Must be one of: {', '.join(valid_sanitizers)}")
        return v

    @validator('error_token')
    def validate_error_token(cls, v):
        # error_token is now optional
        if v is not None and not v.strip():
            raise ValueError("Error token cannot be empty string (use None if not provided)")
        return v.strip() if v else None


class Vulnerability(BaseModel):
    """Vulnerability configuration grouping related POV variants."""

    vuln_keyword: str = Field(..., description="Vulnerability keyword (maps to directory name)")
    difficulty_level: Optional[int] = Field(default=None, ge=1, le=5, description="Intrinsic difficulty level (1-5)")
    povs: List[POV] = Field(..., description="List of POV variants for this vulnerability")

    @validator('vuln_keyword')
    def validate_vuln_keyword(cls, v):
        if not v or not v.strip():
            raise ValueError("Vulnerability keyword cannot be empty")
        return v.strip()

    @validator('povs')
    def validate_povs(cls, v):
        if not v:
            raise ValueError("At least one POV variant must be specified for each vulnerability")

        # Check for duplicate POV IDs
        pov_ids = [pov.id for pov in v]
        if len(pov_ids) != len(set(pov_ids)):
            duplicates = [pov_id for pov_id in pov_ids if pov_ids.count(pov_id) > 1]
            raise ValueError(f"Duplicate POV IDs found: {', '.join(set(duplicates))}")

        return v


class HarnessFile(BaseModel):
    """Harness file configuration."""

    name: str = Field(..., description="Name of the harness")
    path: str = Field(..., description="Path to harness file (absolute path in container)")
    vulns: Optional[List[Vulnerability]] = Field(default_factory=list, description="List of vulnerabilities for this harness")

    @validator('name')
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness name cannot be empty")
        return v.strip()

    @validator('path')
    def validate_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness path cannot be empty")
        # Accept absolute paths (most common in Docker containers)
        # or relative paths starting with ./
        path = v.strip()
        if not (path.startswith('/') or path.startswith('./')):
            raise ValueError("Harness path should be absolute (e.g., /src/project/test/harness.c) or relative (e.g., ./test/harness.c)")
        return path


class DeltaMode(BaseModel):
    """Delta mode configuration with base and ref commits."""

    base_commit: str = Field(..., description="Base commit hash")
    ref_commit: str = Field(..., description="Reference commit hash")

    @validator('base_commit', 'ref_commit')
    def validate_commit_hash(cls, v):
        if not v or not v.strip():
            raise ValueError("Commit hash cannot be empty")

        # Basic git commit hash validation (7-40 hex characters)
        commit_hash = v.strip()
        if not re.match(r'^[a-fA-F0-9]{7,40}$', commit_hash):
            raise ValueError(f"Invalid commit hash format: {commit_hash}")

        return commit_hash


class FullMode(BaseModel):
    """Full mode configuration with single base commit."""

    base_commit: str = Field(..., description="Base commit hash")

    @validator('base_commit')
    def validate_commit_hash(cls, v):
        if not v or not v.strip():
            raise ValueError("Commit hash cannot be empty")

        # Basic git commit hash validation (7-40 hex characters)
        commit_hash = v.strip()
        if not re.match(r'^[a-fA-F0-9]{7,40}$', commit_hash):
            raise ValueError(f"Invalid commit hash format: {commit_hash}")

        return commit_hash


class BenchmarkConfig(BaseModel):
    """Complete benchmark configuration schema."""

    patch_exclude_list: Optional[List[str]] = Field(default_factory=list, description="Files that patches cannot modify")
    delta_mode: Optional[DeltaMode] = Field(default=None, description="Delta mode configuration")
    full_mode: Optional[FullMode] = Field(default=None, description="Full mode configuration")
    harness_files: List[HarnessFile] = Field(..., description="List of harness files")

    @validator('harness_files')
    def validate_harness_files(cls, v):
        if not v:
            raise ValueError("At least one harness file must be specified")

        # Check for duplicate harness names
        names = [harness.name for harness in v]
        if len(names) != len(set(names)):
            duplicates = [name for name in names if names.count(name) > 1]
            raise ValueError(f"Duplicate harness names found: {', '.join(set(duplicates))}")

        return v

    @validator('patch_exclude_list')
    def validate_patch_exclude_list(cls, v):
        if v is None:
            return []

        # Remove empty patterns
        cleaned = [pattern.strip() for pattern in v if pattern and pattern.strip()]
        return cleaned

    def __init__(self, **data):
        super().__init__(**data)

        # Validate that at least one mode is specified
        if not self.delta_mode and not self.full_mode:
            raise ValueError("At least one evaluation mode (delta_mode or full_mode) must be specified")


class Hint(BaseModel):
    """Hint configuration for progressive difficulty control."""

    level: int = Field(..., ge=1, le=4, description="Hint level (1-4)")
    text: str = Field(..., description="Hint text content")
    category: Optional[str] = Field(default=None, description="Hint category")

    @validator('text')
    def validate_text(cls, v):
        if not v or not v.strip():
            raise ValueError("Hint text cannot be empty")
        return v.strip()


class ValidationMetadata(BaseModel):
    """Metadata about the validation process."""

    file_path: Optional[str] = Field(default=None, description="Path to validated file")
    file_size: Optional[int] = Field(default=None, description="Size of validated file")
    yaml_valid: bool = Field(default=False, description="Whether YAML is syntactically valid")
    schema_valid: bool = Field(default=False, description="Whether content matches schema")
    total_harnesses: int = Field(default=0, description="Total number of harnesses")
    total_vulns: int = Field(default=0, description="Total number of vulnerabilities")
    total_povs: int = Field(default=0, description="Total number of POV variants")
    has_delta_mode: bool = Field(default=False, description="Whether delta mode is configured")
    has_full_mode: bool = Field(default=False, description="Whether full mode is configured")
    patch_exclude_patterns: int = Field(default=0, description="Number of patch exclusion patterns")


class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    trials: int = Field(..., ge=1, description="Number of trials (must be >= 1)")
    max_total_time: int = Field(..., ge=1, description="Maximum time in seconds per trial (must be >= 1)")
    difficulty_level: int = Field(..., ge=0, le=4, description="Difficulty level controlling assistance (0-4)")
    experiment_filestore: str = Field(..., description="Directory path for experiment data storage")
    report_filestore: str = Field(..., description="Directory path for HTML reports and summary data")

    @validator('experiment_filestore', 'report_filestore')
    def validate_filestore_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Filestore path cannot be empty")
        return v.strip()


class BenchmarkSuiteConfig(BaseModel):
    """Benchmark suite configuration schema."""

    model_config = {"populate_by_name": True}  # Pydantic V2 syntax

    Name: str = Field(..., description="Unique identifier for the benchmark suite")
    Description: str = Field(..., description="Description of the benchmark suite purpose and scope")
    benchmark_list: List[str] = Field(..., description="List of benchmark IDs included in the suite")

    # Note: "Release date" field name has a space, handling with Field alias
    release_date: str = Field(..., alias="Release date", description="Release date of the benchmark suite")

    @validator('Name')
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Name cannot be empty")
        return v.strip()

    @validator('Description')
    def validate_description(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Description cannot be empty")
        return v.strip()

    @validator('release_date')
    def validate_release_date(cls, v):
        if not v or not v.strip():
            raise ValueError("Release date cannot be empty")

        # Validate date format MM.DD.YYYY
        date_str = v.strip()
        if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
            raise ValueError(f"Invalid release date format: {date_str}. Expected format: MM.DD.YYYY (e.g., 09.23.2025)")

        return date_str

    @validator('benchmark_list')
    def validate_benchmark_list(cls, v):
        if not v:
            raise ValueError("benchmark_list must contain at least one benchmark ID")

        # Check for empty strings
        cleaned = [bid.strip() for bid in v if bid and bid.strip()]
        if len(cleaned) != len(v):
            raise ValueError("benchmark_list contains empty benchmark IDs")

        # Check for duplicates
        if len(cleaned) != len(set(cleaned)):
            duplicates = [bid for bid in cleaned if cleaned.count(bid) > 1]
            raise ValueError(f"Duplicate benchmark IDs found: {', '.join(set(duplicates))}")

        return cleaned