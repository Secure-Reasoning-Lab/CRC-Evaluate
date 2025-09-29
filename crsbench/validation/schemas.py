"""Pydantic schemas for benchmark configuration validation."""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, validator
import re


class POV(BaseModel):
    """Proof of Vulnerability configuration."""

    name: str = Field(..., description="Name of the POV")
    sanitizer: str = Field(..., description="Sanitizer type (address, memory, etc.)")
    error_token: str = Field(..., description="Expected error pattern from sanitizer")
    requires_clean_build: Optional[bool] = Field(default=False, description="Whether POV requires clean build")

    @validator('name')
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("POV name cannot be empty")
        return v.strip()

    @validator('sanitizer')
    def validate_sanitizer(cls, v):
        valid_sanitizers = {'address', 'memory', 'thread', 'undefined', 'leak'}
        if v not in valid_sanitizers:
            raise ValueError(f"Invalid sanitizer: {v}. Must be one of: {', '.join(valid_sanitizers)}")
        return v

    @validator('error_token')
    def validate_error_token(cls, v):
        if not v or not v.strip():
            raise ValueError("Error token cannot be empty")
        return v.strip()


class HarnessFile(BaseModel):
    """Harness file configuration."""

    name: str = Field(..., description="Name of the harness")
    path: str = Field(..., description="Path to harness file")
    povs: Optional[List[POV]] = Field(default_factory=list, description="List of POVs for this harness")

    @validator('name')
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness name cannot be empty")
        return v.strip()

    @validator('path')
    def validate_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness path cannot be empty")
        # Basic path validation - should contain $REPO, $PROJECT, or be relative
        if not (v.startswith('$REPO/') or v.startswith('$PROJECT/') or v.startswith('./')):
            raise ValueError("Harness path should start with '$REPO/', '$PROJECT/', or be a relative path")
        return v.strip()


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
    total_povs: int = Field(default=0, description="Total number of POVs")
    has_delta_mode: bool = Field(default=False, description="Whether delta mode is configured")
    has_full_mode: bool = Field(default=False, description="Whether full mode is configured")
    patch_exclude_patterns: int = Field(default=0, description="Number of patch exclusion patterns")