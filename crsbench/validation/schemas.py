"""Pydantic schemas for benchmark configuration validation."""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
import re


class POV(BaseModel):
    """Proof of Vulnerability configuration."""

    id: str = Field(..., description="POV variant ID (e.g., pov_0, pov_1)")
    sanitizer: str = Field(..., description="Sanitizer type (address, memory, undefined, etc.)")
    error_token: Optional[str] = Field(default=None, description="Expected error pattern from sanitizer (optional)")

    @field_validator('id')
    @classmethod
    def validate_id(cls, v):
        if not v or not v.strip():
            raise ValueError("POV id cannot be empty")
        return v.strip()

    @field_validator('sanitizer')
    @classmethod
    def validate_sanitizer(cls, v):
        valid_sanitizers = {'address', 'memory', 'thread', 'undefined', 'leak'}
        if v not in valid_sanitizers:
            raise ValueError(f"Invalid sanitizer: {v}. Must be one of: {', '.join(valid_sanitizers)}")
        return v

    @field_validator('error_token')
    @classmethod
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

    @field_validator('vuln_keyword')
    @classmethod
    def validate_vuln_keyword(cls, v):
        if not v or not v.strip():
            raise ValueError("Vulnerability keyword cannot be empty")
        return v.strip()

    @field_validator('povs')
    @classmethod
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

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness name cannot be empty")
        return v.strip()

    @field_validator('path')
    @classmethod
    def validate_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness path cannot be empty")

        path = v.strip()

        # Accept path variable patterns
        # $REPO: The cloned repository directory (where source code lives)
        # $PROJECT: The OSS-Fuzz compatible project directory (containing project.yaml, build.sh, etc.)
        if path.startswith('$REPO/') or path.startswith('$PROJECT/'):
            return path

        # Accept absolute paths (most common in Docker containers)
        # or relative paths starting with ./
        if not (path.startswith('/') or path.startswith('./')):
            raise ValueError("Harness path should be one of: $REPO/..., $PROJECT/..., /absolute/path, or ./relative/path")

        return path

    @property
    def povs(self) -> List[POV]:
        """Get flattened list of all POVs from all vulnerabilities."""
        all_povs = []
        for vuln in (self.vulns or []):
            all_povs.extend(vuln.povs)
        return all_povs


class DeltaMode(BaseModel):
    """Delta mode configuration with base and ref commits."""

    base_commit: str = Field(..., description="Base commit hash")
    ref_commit: str = Field(..., description="Reference commit hash")

    @field_validator('base_commit', 'ref_commit')
    @classmethod
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

    @field_validator('base_commit')
    @classmethod
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

    @field_validator('harness_files')
    @classmethod
    def validate_harness_files(cls, v):
        if not v:
            raise ValueError("At least one harness file must be specified")

        # Check for duplicate harness names
        names = [harness.name for harness in v]
        if len(names) != len(set(names)):
            duplicates = [name for name in names if names.count(name) > 1]
            raise ValueError(f"Duplicate harness names found: {', '.join(set(duplicates))}")

        return v

    @field_validator('patch_exclude_list')
    @classmethod
    def validate_patch_exclude_list(cls, v):
        if v is None:
            return []

        # Remove empty patterns
        cleaned = [pattern.strip() for pattern in v if pattern and pattern.strip()]
        return cleaned

    @model_validator(mode='after')
    def check_at_least_one_mode(self):
        """Validate that at least one mode is specified."""
        if not self.delta_mode and not self.full_mode:
            raise ValueError("At least one evaluation mode (delta_mode or full_mode) must be specified")
        return self


class Hint(BaseModel):
    """Hint configuration for progressive difficulty control."""

    level: int = Field(..., ge=1, le=4, description="Hint level (1-4)")
    text: str = Field(..., description="Hint text content")
    category: Optional[str] = Field(default=None, description="Hint category")

    @field_validator('text')
    @classmethod
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

    experiment: str = Field(..., description="Unique identifier for this experiment run")
    trials: int = Field(..., ge=1, description="Number of trials (must be >= 1)")
    max_total_time: int = Field(..., ge=1, description="Maximum time in seconds per trial (must be >= 1)")
    difficulty_level: int = Field(..., ge=0, le=4, description="Difficulty level controlling assistance (0-4)")
    experiment_filestore: str = Field(..., description="Directory path for experiment data storage")
    report_filestore: str = Field(..., description="Directory path for HTML reports and summary data")
    crses: List[str] = Field(..., description="List of CRS implementations to evaluate")
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server hostname or IP (optional, omit or set to 'none' for local mode)"
    )
    benchmarks_root: Optional[str] = Field(
        default=None,
        description="Root directory containing benchmark projects (defaults to ./benchmarks)"
    )
    benchmarks: Optional[List[str]] = Field(
        default=None,
        description="List of benchmark IDs to evaluate (mutually exclusive with benchmark_suite)"
    )
    benchmark_suite: Optional[str] = Field(
        default=None,
        description="Benchmark suite name to load from benchmark-suites/ (mutually exclusive with benchmarks)"
    )
    snapshot_period: Optional[int] = Field(
        default=900,
        ge=0,
        description="Snapshot interval in seconds (0 to disable, default 900 = 15 minutes)"
    )
    registry_dir: Optional[str] = Field(
        default=None,
        description="Path to CRS registry directory (defaults to ./crses/registry)"
    )
    crs_configs_dir: Optional[str] = Field(
        default=None,
        description="Path to CRS configs directory (defaults to ./crses/configs)"
    )
    hints_enabled: bool = Field(
        default=False,
        description="Enable hints for CRS evaluation"
    )
    hint_sarif_level: Optional[int] = Field(
        default=None,
        ge=1, le=5,
        description="SARIF hint level (1=vague, 5=detailed). None disables SARIF hints."
    )
    hint_corpus_level: Optional[int] = Field(
        default=None,
        ge=1, le=5,
        description="Pre-fuzz corpus level (1=minimal, 5=comprehensive). None disables corpus. [PLACEHOLDER - not yet implemented]"
    )
    litellm_mode: Optional[Literal["passthrough", "proxy"]] = Field(
        default="passthrough",
        description="LiteLLM mode: 'passthrough' uses external LiteLLM (UPSTREAM_LITELLM_BASE_URL, LITELLM_API_KEY), "
                    "'proxy' uses self-hosted proxy (LITELLM_BASE_URL, LITELLM_MASTER_KEY). "
                    "Default is 'passthrough'."
    )

    @field_validator('experiment')
    @classmethod
    def validate_experiment(cls, v):
        """Validate experiment name."""
        if not v or not v.strip():
            raise ValueError("Experiment name cannot be empty")
        return v.strip()

    @field_validator('crses')
    @classmethod
    def validate_crses(cls, v):
        """Validate CRS list."""
        if not v:
            raise ValueError("At least one CRS must be specified")

        # Check for empty strings
        cleaned = [crs.strip() for crs in v if crs and crs.strip()]
        if len(cleaned) != len(v):
            raise ValueError("crses list contains empty CRS names")

        # Check for duplicates
        if len(cleaned) != len(set(cleaned)):
            duplicates = [crs for crs in cleaned if cleaned.count(crs) > 1]
            raise ValueError(f"Duplicate CRS names found: {', '.join(set(duplicates))}")

        return cleaned

    @field_validator('experiment_filestore', 'report_filestore')
    @classmethod
    def validate_filestore_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Filestore path cannot be empty")
        return v.strip()

    @field_validator('redis_host')
    @classmethod
    def validate_redis_host(cls, v):
        """Validate Redis host field."""
        if v and v.strip() and v.strip().lower() != 'none':
            return v.strip()
        return None  # Treat empty or "none" as None (local mode)

    @field_validator('benchmarks_root')
    @classmethod
    def validate_benchmarks_root(cls, v):
        """Validate benchmarks root directory."""
        if v and v.strip():
            from pathlib import Path
            path = Path(v.strip())
            if not path.exists():
                raise ValueError(f"Benchmarks root directory does not exist: {v}")
            if not path.is_dir():
                raise ValueError(f"Benchmarks root must be a directory: {v}")
            return str(path.absolute())
        return None  # Use default ./benchmarks if not specified

    @field_validator('benchmarks')
    @classmethod
    def validate_benchmarks(cls, v):
        """Validate benchmarks list."""
        if v is None:
            return None

        if not isinstance(v, list):
            raise ValueError("benchmarks must be a list")

        # Check for empty strings
        cleaned = [bid.strip() for bid in v if bid and bid.strip()]
        if len(cleaned) != len(v):
            raise ValueError("benchmarks list contains empty benchmark IDs")

        # Check for duplicates
        if len(cleaned) != len(set(cleaned)):
            duplicates = [bid for bid in cleaned if cleaned.count(bid) > 1]
            raise ValueError(f"Duplicate benchmark IDs found: {', '.join(set(duplicates))}")

        return cleaned if cleaned else None

    @field_validator('benchmark_suite')
    @classmethod
    def validate_benchmark_suite(cls, v):
        """Validate benchmark_suite format."""
        if v is None:
            return None

        # Validate suite name format
        suite_name = v.strip()
        if not suite_name:
            raise ValueError("benchmark_suite cannot be empty string")

        return suite_name

    @field_validator('snapshot_period')
    @classmethod
    def validate_snapshot_period(cls, v):
        """Validate snapshot period."""
        if v is None:
            return 900  # Default: 15 minutes

        if v == 0:
            return 0  # Disabled

        if v < 60:
            raise ValueError("snapshot_period must be at least 60 seconds (or 0 to disable)")

        if v > 86400:
            raise ValueError(f"snapshot_period of {v}s (>{v/3600:.1f} hours) exceeds maximum of 24 hours")

        return v

    @field_validator('litellm_mode')
    @classmethod
    def validate_litellm_mode(cls, v):
        """Validate LiteLLM mode."""
        if v is not None and v not in ('passthrough', 'proxy'):
            raise ValueError(f"Invalid litellm_mode: {v}. Must be 'passthrough' or 'proxy'")
        return v

    @model_validator(mode='after')
    def check_benchmarks_configuration(self):
        """Ensure benchmarks configuration is valid."""
        # Check mutual exclusivity
        if self.benchmarks is not None and self.benchmark_suite is not None:
            raise ValueError("Cannot specify both 'benchmarks' and 'benchmark_suite'. Please use only one.")

        # Ensure at least one is specified
        if self.benchmarks is None and self.benchmark_suite is None:
            raise ValueError("Either 'benchmarks' or 'benchmark_suite' must be specified")

        return self

    @model_validator(mode='after')
    def check_hints_configuration(self):
        """Validate hint configuration consistency."""
        if self.hints_enabled:
            if self.hint_sarif_level is None and self.hint_corpus_level is None:
                raise ValueError(
                    "hints_enabled=True requires at least one of hint_sarif_level or hint_corpus_level to be set"
                )
        return self

    def get_benchmark_list(self, benchmark_suites_dir: str = "benchmark-suites") -> List[str]:
        """Get the list of benchmarks, resolving benchmark_suite if necessary.

        Args:
            benchmark_suites_dir: Directory containing benchmark suite YAML files

        Returns:
            List of benchmark IDs

        Raises:
            ValueError: If benchmark_suite file doesn't exist or is invalid
        """
        if self.benchmarks is not None:
            return self.benchmarks

        if self.benchmark_suite is not None:
            from pathlib import Path
            import yaml

            # Construct path to suite file
            suite_path = Path(benchmark_suites_dir) / f"{self.benchmark_suite}.yaml"

            if not suite_path.exists():
                raise ValueError(f"Benchmark suite file not found: {suite_path}")

            # Load and validate suite file
            with open(suite_path, 'r') as f:
                suite_data = yaml.safe_load(f)

            # Validate using BenchmarkSuiteConfig schema
            suite_config = BenchmarkSuiteConfig(**suite_data)

            return suite_config.benchmark_list

        # Should never reach here due to __init__ validation
        raise ValueError("No benchmark source specified")

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary for job serialization."""
        return {
            'experiment': self.experiment,
            'trials': self.trials,
            'max_total_time': self.max_total_time,
            'difficulty_level': self.difficulty_level,
            'experiment_filestore': self.experiment_filestore,
            'report_filestore': self.report_filestore,
            'crses': self.crses,
            'redis_host': self.redis_host,
            'benchmarks_root': self.benchmarks_root,
            'benchmarks': self.benchmarks,
            'benchmark_suite': self.benchmark_suite,
            'hints_enabled': self.hints_enabled,
            'hint_sarif_level': self.hint_sarif_level,
            'hint_corpus_level': self.hint_corpus_level,
            'litellm_mode': self.litellm_mode,
        }


class BenchmarkSuiteConfig(BaseModel):
    """Benchmark suite configuration schema."""

    model_config = {"populate_by_name": True}  # Pydantic V2 syntax

    Name: str = Field(..., description="Unique identifier for the benchmark suite")
    Description: str = Field(..., description="Description of the benchmark suite purpose and scope")
    benchmark_list: List[str] = Field(..., description="List of benchmark IDs included in the suite")

    # Note: "Release date" field name has a space, handling with Field alias
    release_date: str = Field(..., alias="Release date", description="Release date of the benchmark suite")

    @field_validator('Name')
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Name cannot be empty")
        return v.strip()

    @field_validator('Description')
    @classmethod
    def validate_description(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Description cannot be empty")
        return v.strip()

    @field_validator('release_date')
    @classmethod
    def validate_release_date(cls, v):
        if not v or not v.strip():
            raise ValueError("Release date cannot be empty")

        # Validate date format MM.DD.YYYY
        date_str = v.strip()
        if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
            raise ValueError(f"Invalid release date format: {date_str}. Expected format: MM.DD.YYYY (e.g., 09.23.2025)")

        return date_str

    @field_validator('benchmark_list')
    @classmethod
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