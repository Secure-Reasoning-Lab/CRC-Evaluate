"""Pydantic schemas for CRSBench.

This module contains:
1. Shared schemas for cross-module data contracts (evaluation -> reporting)
2. Benchmark configuration validation schemas
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# =============================================================================
# Shared Schemas (used across evaluation and reporting modules)
# =============================================================================


class TrialMode(str, Enum):
    """Trial execution mode.

    - bug_finding: CRS discovers vulnerabilities (POVs)
    - patch_generation: CRS generates patches for known vulnerabilities
    - unknown: Mode not specified (metadata missing)
    """

    bug_finding = "bug_finding"
    patch_generation = "patch_generation"
    unknown = "unknown"


class SourceInfo(BaseModel):
    """Source code information in trial metadata."""

    model_config = ConfigDict(extra="allow")

    path: str
    commit: str | None = None


class HintsStats(BaseModel):
    """Statistics about prepared hints."""

    model_config = ConfigDict(extra="allow")

    path: str
    sarif_count: int = 0
    corpus_count: int = 0
    has_ref_diff: bool = False


class PovsStats(BaseModel):
    """Statistics about prepared POVs."""

    model_config = ConfigDict(extra="allow")

    path: str
    pov_count: int = 0


class TrialConfig(BaseModel):
    """Trial configuration settings."""

    model_config = ConfigDict(extra="allow")

    hints_enabled: bool = False
    hints_corpus_level: int | None = None
    target_povs: int | None = None


class FrameworkInfo(BaseModel):
    """Framework version and commit information."""

    model_config = ConfigDict(extra="allow")

    version: str = Field(description="CRSBench version")
    crsbench_commit: str = Field(description="CRSBench git commit")
    oss_crs_commit: str = Field(description="oss-crs submodule commit")
    oss_fuzz_commit: str = Field(description="oss-fuzz submodule commit")
    build_timestamp: str | None = Field(default=None, description="Build timestamp")


class TrialMetadata(BaseModel):
    """Trial metadata from metadata.json file.

    This is the schema for metadata.json in trial directories.
    Written by: evaluation/trial_preparation.py
    Read by: reporting/snapshot_loader.py
    """

    model_config = ConfigDict(extra="allow")

    timestamp: str
    trial_num: int
    crs: str
    benchmark: str
    harness: str
    mode: TrialMode
    source: SourceInfo
    hints: HintsStats | None = None
    povs: PovsStats | None = None
    config: TrialConfig = Field(default_factory=TrialConfig)
    framework: FrameworkInfo | None = Field(
        default=None, description="CRSBench framework version and commit info"
    )


class SnapshotMetadata(BaseModel):
    """Snapshot metadata from metadata.json inside tar.gz archive.

    This is the schema for metadata.json inside snapshot-XXXX.tar.gz.
    Written by: evaluation/snapshot_manager.py
    Read by: reporting/snapshot_loader.py
    """

    model_config = ConfigDict(extra="allow")

    cycle: int
    timestamp: float
    elapsed_time: float
    snapshot_period: int
    running_elapsed_time: float | None = None


class ModelUsage(BaseModel):
    """Usage statistics for a single LLM model."""

    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class LLMUsage(BaseModel):
    """LLM usage data from llm-usage.json file.

    Written by: CRS during execution
    Read by: reporting/snapshot_loader.py
    """

    model_config = ConfigDict(extra="allow")

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_cost_usd: float = 0.0
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)


# =============================================================================
# Benchmark Configuration Schemas
# =============================================================================


class EvaluationMode(str, Enum):
    """Evaluation mode for benchmark execution."""

    DELTA = "delta"
    FULL = "full"
    ALL = "all"


class RtsMode(str, Enum):
    """Regression Test Selection mode for Java projects."""

    NONE = "none"  # Explicitly disabled (prefer null in YAML)
    JCGEKS = "jcgeks"
    OPENCLOVER = "openclover"
    BINARYRTS = "binaryrts"


@dataclass
class BenchmarkEntry:
    """Represents a benchmark with optional harness specification.

    Attributes:
        name: Benchmark identifier (e.g., afc-curl-delta-01)
        harnesses: Optional list of specific harnesses to run.
                   If None, all harnesses for this benchmark will be used.
    """

    name: str
    harnesses: Optional[List[str]] = None


@dataclass
class BenchmarkHarness:
    """A resolved benchmark-harness pair for trial execution.

    This represents a single harness within a benchmark, ready for trial execution.
    Created by resolve_benchmark_harnesses() after validating harness exists.

    Attributes:
        name: Benchmark name (e.g., "afc-curl-delta-01")
        path: Path to benchmark directory
        harness: Parsed harness with name and path
    """

    name: str
    path: Path
    harness: "HarnessFile"  # Forward reference - HarnessFile defined later in this file


class POV(BaseModel):
    """Proof of Vulnerability configuration."""

    id: str = Field(..., description="POV variant ID (e.g., pov_0, pov_1)")
    sanitizer: str = Field(
        ..., description="Sanitizer type (address, memory, undefined, etc.)"
    )
    error_token: Optional[str] = Field(
        default=None, description="Expected error pattern from sanitizer (optional)"
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, v):
        if not v or not v.strip():
            raise ValueError("POV id cannot be empty")
        return v.strip()

    @field_validator("sanitizer")
    @classmethod
    def validate_sanitizer(cls, v):
        valid_sanitizers = {"address", "memory", "thread", "undefined", "leak"}
        if v not in valid_sanitizers:
            raise ValueError(
                f"Invalid sanitizer: {v}. Must be one of: {', '.join(valid_sanitizers)}"
            )
        return v

    @field_validator("error_token")
    @classmethod
    def validate_error_token(cls, v):
        # error_token is now optional
        if v is not None and not v.strip():
            raise ValueError(
                "Error token cannot be empty string (use None if not provided)"
            )
        return v.strip() if v else None


class Vulnerability(BaseModel):
    """Vulnerability configuration grouping related POV variants."""

    vuln_keyword: str = Field(
        ..., description="Vulnerability keyword (maps to directory name)"
    )
    difficulty_level: Optional[int] = Field(
        default=None, ge=1, le=5, description="Intrinsic difficulty level (1-5)"
    )
    povs: List[POV] = Field(
        ..., description="List of POV variants for this vulnerability"
    )

    @field_validator("vuln_keyword")
    @classmethod
    def validate_vuln_keyword(cls, v):
        if not v or not v.strip():
            raise ValueError("Vulnerability keyword cannot be empty")
        return v.strip()

    @field_validator("povs")
    @classmethod
    def validate_povs(cls, v):
        if not v:
            raise ValueError(
                "At least one POV variant must be specified for each vulnerability"
            )

        # Check for duplicate POV IDs
        pov_ids = [pov.id for pov in v]
        if len(pov_ids) != len(set(pov_ids)):
            duplicates = [pov_id for pov_id in pov_ids if pov_ids.count(pov_id) > 1]
            raise ValueError(f"Duplicate POV IDs found: {', '.join(set(duplicates))}")

        return v


class HarnessFile(BaseModel):
    """Harness file configuration."""

    name: str = Field(..., description="Name of the harness")
    path: str = Field(
        ..., description="Path to harness file (absolute path in container)"
    )
    vulns: Optional[List[Vulnerability]] = Field(
        default_factory=list, description="List of vulnerabilities for this harness"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness name cannot be empty")
        return v.strip()

    @field_validator("path")
    @classmethod
    def validate_path(cls, v):
        if not v or not v.strip():
            raise ValueError("Harness path cannot be empty")

        path = v.strip()

        # Accept path variable patterns
        # $REPO: The cloned repository directory (where source code lives)
        # $PROJECT: The OSS-Fuzz compatible project directory (containing project.yaml, build.sh, etc.)
        if path.startswith(("$REPO/", "$PROJECT/")):
            return path

        # Accept absolute paths (most common in Docker containers)
        # or relative paths starting with ./
        if not (path.startswith(("/", "./"))):
            raise ValueError(
                "Harness path should be one of: $REPO/..., $PROJECT/..., /absolute/path, or ./relative/path"
            )

        return path

    @property
    def povs(self) -> List[POV]:
        """Get flattened list of all POVs from all vulnerabilities."""
        all_povs = []
        for vuln in self.vulns or []:
            all_povs.extend(vuln.povs)
        return all_povs


# ============================================================================
# Vulnerability Metadata Models (for vuln.yaml files)
# ============================================================================


class VulnerabilityLocation(BaseModel):
    """Location of vulnerable code in the repository."""

    type: Optional[Literal["crash_site", "root_cause"]] = Field(
        default=None,
        description=(
            "Type of location: 'crash_site' (where crash occurs, excluding 3rd-party "
            "libraries) or 'root_cause' (underlying defect that enables vulnerability)"
        ),
    )
    path_from_root: str = Field(description="Path to the file from repository root")
    function_name: Optional[str] = Field(
        default=None, description="Name of the function containing the vulnerability"
    )
    startLine: int = Field(  # noqa: N815 - RFC schema compatibility
        description="Starting line number of the vulnerable code"
    )
    startColumn: int = Field(  # noqa: N815 - RFC schema compatibility
        description="Starting column number of the vulnerable code"
    )
    endLine: int = Field(  # noqa: N815 - RFC schema compatibility
        description="Ending line number of the vulnerable code"
    )
    endColumn: int = Field(  # noqa: N815 - RFC schema compatibility
        description="Ending column number of the vulnerable code"
    )

    model_config = {"extra": "allow"}


class VulnerabilityMetadata(BaseModel):
    """Complete vulnerability metadata in RFC format (for vuln.yaml files)."""

    id: str = Field(description="Vulnerability identifier (e.g., cpv_0, cpv_1)")
    name: str = Field(description="Human-readable name for the vulnerability")
    author: Optional[str] = Field(
        default=None,
        description="Author who created or labeled this vulnerability metadata",
    )
    validator: Optional[str] = Field(
        default=None,
        description="Validator who verified this vulnerability metadata",
    )
    origin: Optional[Literal["1-day", "synthetic"]] = Field(
        default=None,
        description="Origin of vulnerability: '1-day' (real CVE) or 'synthetic' (artificially created)",
    )
    release_date: Optional[str] = Field(
        default=None,
        description="Vulnerability disclosure date in MM/DD/YYYY format",
    )
    references: List[str] = Field(
        default_factory=list,
        description="URLs to vulnerability details (CVE, OSV, GitHub advisory, bug tracker)",
    )
    cwes: List[str] = Field(
        default_factory=list,
        description="List of CWE identifiers associated with this vulnerability",
    )
    description: str = Field(description="Detailed description of the vulnerability")
    locations: List[VulnerabilityLocation] = Field(
        default_factory=list,
        description="Code locations where the vulnerability exists",
    )

    model_config = {"extra": "allow"}

    def to_yaml(self, output_path: Path) -> None:
        """Write vulnerability metadata to YAML file with proper formatting."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(exclude_none=False)

        yaml.add_representer(
            type(None),
            lambda dumper, _value: dumper.represent_scalar(
                "tag:yaml.org,2002:null", "null"
            ),
        )

        yaml_str = yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            width=120,
        )
        yaml_str = self._add_section_breaks(yaml_str)

        with output_path.open("w") as f:
            f.write(yaml_str)

    def _add_section_breaks(self, yaml_str: str) -> str:
        """Add line breaks between major sections for better readability."""
        lines = yaml_str.split("\n")
        result = []
        prev_line = ""

        for line in lines:
            if (
                line
                and not line.startswith(" ")
                and not line.startswith("-")
                and result
                and prev_line
            ):
                if prev_line.strip():
                    result.append("")
            result.append(line)
            prev_line = line

        return "\n".join(result)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VulnerabilityMetadata":
        """Create VulnerabilityMetadata from dictionary."""
        return cls(**data)

    @classmethod
    def from_yaml(cls, input_path: Path) -> "VulnerabilityMetadata":
        """Load vulnerability metadata from YAML file."""
        with input_path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)


class DeltaMode(BaseModel):
    """Delta mode configuration with base and ref commits."""

    base_commit: str = Field(..., description="Base commit hash")
    ref_commit: str = Field(..., description="Reference commit hash")

    @field_validator("base_commit", "ref_commit")
    @classmethod
    def validate_commit_hash(cls, v):
        if not v or not v.strip():
            raise ValueError("Commit hash cannot be empty")

        # Basic git commit hash validation (7-40 hex characters)
        commit_hash = v.strip()
        if not re.match(r"^[a-fA-F0-9]{7,40}$", commit_hash):
            raise ValueError(f"Invalid commit hash format: {commit_hash}")

        return commit_hash


class FullMode(BaseModel):
    """Full mode configuration with single base commit."""

    base_commit: str = Field(..., description="Base commit hash")

    @field_validator("base_commit")
    @classmethod
    def validate_commit_hash(cls, v):
        if not v or not v.strip():
            raise ValueError("Commit hash cannot be empty")

        # Basic git commit hash validation (7-40 hex characters)
        commit_hash = v.strip()
        if not re.match(r"^[a-fA-F0-9]{7,40}$", commit_hash):
            raise ValueError(f"Invalid commit hash format: {commit_hash}")

        return commit_hash


class BenchmarkConfig(BaseModel):
    """Complete benchmark configuration schema."""

    patch_exclude_list: Optional[List[str]] = Field(
        default_factory=list, description="Files that patches cannot modify"
    )
    delta_mode: Optional[DeltaMode] = Field(
        default=None, description="Delta mode configuration"
    )
    full_mode: Optional[FullMode] = Field(
        default=None, description="Full mode configuration"
    )
    harness_files: List[HarnessFile] = Field(..., description="List of harness files")

    @field_validator("harness_files")
    @classmethod
    def validate_harness_files(cls, v):
        if not v:
            raise ValueError("At least one harness file must be specified")

        # Check for duplicate harness names
        names = [harness.name for harness in v]
        if len(names) != len(set(names)):
            duplicates = [name for name in names if names.count(name) > 1]
            raise ValueError(
                f"Duplicate harness names found: {', '.join(set(duplicates))}"
            )

        return v

    @field_validator("patch_exclude_list")
    @classmethod
    def validate_patch_exclude_list(cls, v):
        if v is None:
            return []

        # Remove empty patterns
        return [pattern.strip() for pattern in v if pattern and pattern.strip()]

    @model_validator(mode="after")
    def check_at_least_one_mode(self):
        """Validate that at least one mode is specified."""
        if not self.delta_mode and not self.full_mode:
            raise ValueError(
                "At least one evaluation mode (delta_mode or full_mode) must be specified"
            )
        return self

    def to_yaml(self, output_path: Path) -> None:
        """Write benchmark configuration to YAML file (meta.yaml format)."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(exclude_none=True)

        yaml_str = yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            width=120,
        )

        with output_path.open("w") as f:
            f.write(yaml_str)


class Hint(BaseModel):
    """Hint configuration for progressive difficulty control."""

    level: int = Field(..., ge=1, le=4, description="Hint level (1-4)")
    text: str = Field(..., description="Hint text content")
    category: Optional[str] = Field(default=None, description="Hint category")

    @field_validator("text")
    @classmethod
    def validate_text(cls, v):
        if not v or not v.strip():
            raise ValueError("Hint text cannot be empty")
        return v.strip()


class ValidationMetadata(BaseModel):
    """Metadata about the validation process."""

    file_path: Optional[str] = Field(default=None, description="Path to validated file")
    file_size: Optional[int] = Field(default=None, description="Size of validated file")
    yaml_valid: bool = Field(
        default=False, description="Whether YAML is syntactically valid"
    )
    schema_valid: bool = Field(
        default=False, description="Whether content matches schema"
    )
    total_harnesses: int = Field(default=0, description="Total number of harnesses")
    total_vulns: int = Field(default=0, description="Total number of vulnerabilities")
    total_povs: int = Field(default=0, description="Total number of POV variants")
    has_delta_mode: bool = Field(
        default=False, description="Whether delta mode is configured"
    )
    has_full_mode: bool = Field(
        default=False, description="Whether full mode is configured"
    )
    patch_exclude_patterns: int = Field(
        default=0, description="Number of patch exclusion patterns"
    )


class LitellmResourceConfig(BaseModel):
    """LiteLLM resource configuration."""

    max_concurrent_requests: int = Field(
        default=10, ge=1, description="Maximum concurrent requests to LiteLLM"
    )
    cost_budget: float = Field(default=100.0, ge=0, description="Cost budget in USD")


class ResourceConfig(BaseModel):
    """Resource allocation configuration for trials."""

    cores_per_trial: int = Field(
        default=4, ge=1, description="Number of CPU cores allocated per trial"
    )
    memory_per_trial: str = Field(
        default="8G", description="Memory allocation per trial (e.g., '8G', '16G')"
    )
    litellm: Optional[LitellmResourceConfig] = Field(
        default=None, description="LiteLLM resource configuration"
    )


class WorkerConfig(BaseModel):
    """Distributed worker configuration."""

    jobs: int = Field(default=4, ge=1, description="Number of parallel jobs per worker")
    redis_host: str = Field(
        default="localhost", description="Redis server hostname or IP for workers"
    )
    continuous: bool = Field(
        default=True,
        description="Whether workers should run continuously or exit after one job",
    )
    experiment_filestore: Optional[Path] = Field(
        default=None,
        description="Override experiment data storage path for workers on different machines",
    )
    report_filestore: Optional[Path] = Field(
        default=None,
        description="Override report storage path for workers on different machines",
    )
    oss_fuzz_path: Optional[Path] = Field(
        default=None,
        description="Override oss-fuzz path for workers on different machines",
    )
    registry_dir: Optional[Path] = Field(
        default=None,
        description="Override registry directory for workers on different machines",
    )
    crs_configs_dir: Optional[Path] = Field(
        default=None,
        description="Override CRS configs directory for workers on different machines",
    )
    benchmarks_root: Optional[Path] = Field(
        default=None,
        description="Override benchmarks root directory for workers on different machines",
    )
    benchmark_suites_root: Optional[Path] = Field(
        default=None,
        description="Override benchmark suites root directory for workers on different machines",
    )


class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    experiment: str = Field(
        ..., description="Unique identifier for this experiment run"
    )
    trials: int = Field(..., ge=1, description="Number of trials (must be >= 1)")
    mode: EvaluationMode = Field(
        ...,
        description="Evaluation mode: 'delta', 'full', or 'all' (run all available)",
    )
    max_total_time: int = Field(
        ..., ge=1, description="Maximum time in seconds per trial (must be >= 1)"
    )
    build_timeout: int = Field(
        default=3600,
        ge=1,
        description="Maximum time in seconds for CRS build phase (default: 3600 = 1 hour)",
    )
    run_timeout: int = Field(
        default=7200,
        ge=1,
        description="Maximum time in seconds for CRS run phase (default: 7200 = 2 hours)",
    )
    verify_timeout: int = Field(
        default=7200,
        ge=1,
        description="Maximum time in seconds for POV verification phase (default: 7200 = 2 hours)",
    )
    difficulty_level: int = Field(
        ..., ge=0, le=4, description="Difficulty level controlling assistance (0-4)"
    )
    experiment_filestore: Path = Field(
        ..., description="Directory path for experiment data storage"
    )
    report_filestore: Path = Field(
        ..., description="Directory path for HTML reports and summary data"
    )
    crses: List[str] = Field(..., description="List of CRS implementations to evaluate")
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server hostname or IP (optional, omit or set to 'none' for local mode)",
    )
    benchmarks_root: Optional[Path] = Field(
        default=None,
        description="Root directory containing benchmark projects (defaults to ./benchmarks)",
    )
    benchmarks: Optional[List[Union[str, Dict[str, List[str]]]]] = Field(
        default=None,
        description="List of benchmarks to evaluate (mutually exclusive with benchmark_suite). "
        "Each entry can be either a string (benchmark name, runs all harnesses) "
        "or a dict mapping benchmark name to list of specific harnesses to run.",
    )
    benchmark_suite: Optional[str] = Field(
        default=None,
        description="Benchmark suite name to load (mutually exclusive with benchmarks)",
    )
    benchmark_suites_root: Optional[Path] = Field(
        default=None,
        description="Root directory containing benchmark suite YAML files (defaults to ./benchmark-suites)",
    )
    snapshot_period: Optional[int] = Field(
        default=900,
        ge=0,
        description="Snapshot interval in seconds (0 to disable, default 900 = 15 minutes)",
    )
    registry_dir: Optional[Path] = Field(
        default=None,
        description="Path to CRS registry directory (defaults to ./crses/registry)",
    )
    crs_configs_dir: Optional[Path] = Field(
        default=None,
        description="Path to CRS configs directory (defaults to ./crses/configs)",
    )
    hints_enabled: bool = Field(
        default=False, description="Enable hints for CRS evaluation"
    )
    hint_sarif_level: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="SARIF hint level (1=vague, 5=detailed). None disables SARIF hints.",
    )
    hint_corpus_level: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="Pre-fuzz corpus level (1=minimal, 5=comprehensive). None disables corpus. [PLACEHOLDER - not yet implemented]",
    )
    litellm_mode: Optional[Literal["passthrough", "proxy"]] = Field(
        default="passthrough",
        description="LiteLLM mode: 'passthrough' uses external LiteLLM (UPSTREAM_LITELLM_BASE_URL, LITELLM_API_KEY), "
        "'proxy' uses self-hosted proxy (LITELLM_BASE_URL, LITELLM_MASTER_KEY). "
        "Default is 'passthrough'.",
    )
    llm_tracking_enabled: bool = Field(
        default=True,
        description="Enable LLM usage tracking via LiteLLM Virtual Keys. "
        "Automatically enabled when LITELLM_BASE_URL and LITELLM_MASTER_KEY env vars are set. "
        "Set to false to explicitly disable. "
        "Generates llm-usage.json with per-trial cost and token metrics.",
    )
    project_image_prefix: str = Field(
        default="aixcc-afc",
        description="Docker image prefix for custom project images (default: aixcc-afc)",
    )
    skip_verification: bool = Field(
        default=False,
        description="Skip POV verification after CRS execution (default: False, verification enabled)",
    )
    oss_fuzz_path: Path = Field(
        default=Path("oss-fuzz"),
        description="Path to oss-fuzz directory (default: oss-fuzz)",
    )
    coverage_enabled: bool = Field(
        default=False,
        description="Enable coverage collection during trials (default: False). "
        "Use 'crsbench coverage' CLI for post-analysis.",
    )
    coverage_saturation_time: int = Field(
        default=21600,
        ge=0,
        description="Time in seconds without new coverage before saturation (default: 21600 = 6 hours)",
    )
    coverage_early_stop: bool = Field(
        default=False,
        description="Terminate trial early when coverage saturation is detected (default: False). "
        "NOT YET IMPLEMENTED - currently only detects and logs saturation.",
    )
    pov_early_stop: bool = Field(
        default=False,
        description="Terminate trial early when all CPVs for harness are found (default: False).",
    )
    build_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of parallel workers for building variants (default: 4). "
        "CLI --build-workers takes precedence, then CRSBENCH_BUILD_WORKERS env var, then this config value.",
    )
    verify_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of parallel workers for POV/patch verification (default: 4). "
        "CLI --verify-workers takes precedence, then CRSBENCH_VERIFY_WORKERS env var, then this config value.",
    )
    only_cpv_harnesses: bool = Field(
        default=True,
        description="Skip harnesses without CPVs for bug-finding CRS (default: True). "
        "Bug-fixing CRS always skips harnesses without CPVs regardless of this setting.",
    )
    resources: Optional[ResourceConfig] = Field(
        default=None, description="Resource allocation configuration for trials"
    )
    worker: Optional[WorkerConfig] = Field(
        default=None, description="Distributed worker configuration"
    )

    @field_validator("experiment")
    @classmethod
    def validate_experiment(cls, v):
        """Validate experiment name."""
        if not v or not v.strip():
            raise ValueError("Experiment name cannot be empty")
        return v.strip()

    @field_validator("crses")
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

    @field_validator("experiment_filestore", "report_filestore")
    @classmethod
    def validate_filestore_path(cls, v):
        # Pydantic converts str to Path automatically
        # Check that the path is not empty
        path_str = str(v).strip()
        if not path_str or path_str == "" or path_str == ".":
            raise ValueError("Filestore path cannot be empty")
        return v

    @field_validator("redis_host")
    @classmethod
    def validate_redis_host(cls, v):
        """Validate Redis host field."""
        if v and v.strip() and v.strip().lower() != "none":
            return v.strip()
        return None  # Treat empty or "none" as None (local mode)

    @field_validator("benchmarks_root")
    @classmethod
    def validate_benchmarks_root(cls, v):
        """Validate benchmarks root directory."""
        if v:
            # Pydantic already converts str to Path
            if not v.exists():
                raise ValueError(f"Benchmarks root directory does not exist: {v}")
            if not v.is_dir():
                raise ValueError(f"Benchmarks root must be a directory: {v}")
            return v.absolute()
        return None  # Use default ./benchmarks if not specified

    @field_validator("benchmark_suites_root")
    @classmethod
    def validate_benchmark_suites_root(cls, v):
        """Validate benchmark suites root directory."""
        if v:
            # Pydantic already converts str to Path
            if not v.exists():
                raise ValueError(f"Benchmark suites root directory does not exist: {v}")
            if not v.is_dir():
                raise ValueError(f"Benchmark suites root must be a directory: {v}")
            return v.absolute()
        return None  # Use default ./benchmark-suites if not specified

    @field_validator("benchmarks")
    @classmethod
    def validate_benchmarks(cls, v):
        """Validate benchmarks list (supports both string and dict formats)."""
        if v is None:
            return None

        if not isinstance(v, list):
            raise ValueError("benchmarks must be a list")

        cleaned_list = []
        benchmark_names = []

        for item in v:
            if isinstance(item, str):
                # Simple string format
                name = item.strip()
                if not name:
                    raise ValueError("benchmarks list contains empty benchmark ID")
                benchmark_names.append(name)
                cleaned_list.append(name)
            elif isinstance(item, dict):
                # Dict format: {benchmark_name: [harness_list]}
                if len(item) != 1:
                    raise ValueError(
                        "Each dict entry must have exactly one benchmark name as key"
                    )
                name = list(item.keys())[0].strip()
                harnesses = item[name]

                if not name:
                    raise ValueError("Benchmark name cannot be empty")
                if not isinstance(harnesses, list) or not harnesses:
                    raise ValueError(
                        f"Harness list for '{name}' must be a non-empty list"
                    )
                if any(not h or not str(h).strip() for h in harnesses):
                    raise ValueError(
                        f"Harness list for '{name}' contains empty harness names"
                    )

                benchmark_names.append(name)
                # Store normalized format
                cleaned_list.append({name: [str(h).strip() for h in harnesses]})
            else:
                raise ValueError(
                    f"Invalid benchmarks entry type: {type(item).__name__}. "
                    "Expected str or dict"
                )

        # Check for duplicate benchmark names
        if len(benchmark_names) != len(set(benchmark_names)):
            duplicates = [
                name for name in benchmark_names if benchmark_names.count(name) > 1
            ]
            raise ValueError(
                f"Duplicate benchmark IDs found: {', '.join(set(duplicates))}"
            )

        return cleaned_list if cleaned_list else None

    @field_validator("benchmark_suite")
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

    @field_validator("snapshot_period")
    @classmethod
    def validate_snapshot_period(cls, v):
        """Validate snapshot period."""
        if v is None:
            return 900  # Default: 15 minutes

        if v == 0:
            return 0  # Disabled

        if v < 60:
            raise ValueError(
                "snapshot_period must be at least 60 seconds (or 0 to disable)"
            )

        if v > 86400:
            raise ValueError(
                f"snapshot_period of {v}s (>{v / 3600:.1f} hours) exceeds maximum of 24 hours"
            )

        return v

    @field_validator("litellm_mode")
    @classmethod
    def validate_litellm_mode(cls, v):
        """Validate LiteLLM mode."""
        if v is not None and v not in ("passthrough", "proxy"):
            raise ValueError(
                f"Invalid litellm_mode: {v}. Must be 'passthrough' or 'proxy'"
            )
        return v

    @model_validator(mode="after")
    def check_benchmarks_configuration(self):
        """Ensure benchmarks configuration is valid."""
        # Check mutual exclusivity
        if self.benchmarks is not None and self.benchmark_suite is not None:
            raise ValueError(
                "Cannot specify both 'benchmarks' and 'benchmark_suite'. Please use only one."
            )

        # Ensure at least one is specified
        if self.benchmarks is None and self.benchmark_suite is None:
            raise ValueError(
                "Either 'benchmarks' or 'benchmark_suite' must be specified"
            )

        return self

    @model_validator(mode="after")
    def check_hints_configuration(self):
        """Validate hint configuration consistency."""
        if self.hints_enabled:
            if self.hint_sarif_level is None and self.hint_corpus_level is None:
                raise ValueError(
                    "hints_enabled=True requires at least one of hint_sarif_level or hint_corpus_level to be set"
                )
        return self

    @model_validator(mode="after")
    def check_max_total_time_sufficient(self):
        """Validate that max_total_time is sufficient for all phases."""
        min_required = self.build_timeout + self.run_timeout + self.verify_timeout
        if self.max_total_time <= min_required:
            raise ValueError(
                f"max_total_time ({self.max_total_time}s) must be greater than "
                f"build_timeout + run_timeout + verify_timeout ({min_required}s = "
                f"{self.build_timeout} + {self.run_timeout} + {self.verify_timeout})"
            )
        return self

    def get_benchmark_list(self) -> List[str]:
        """Get the list of benchmarks, resolving benchmark_suite if necessary.

        Returns:
            List of benchmark IDs

        Raises:
            ValueError: If benchmark_suite file doesn't exist or is invalid
        """
        if self.benchmarks is not None:
            # Extract benchmark names (handle both str and dict formats)
            names = []
            for item in self.benchmarks:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    names.append(list(item.keys())[0])
            return names

        if self.benchmark_suite is not None:
            from pathlib import Path

            import yaml

            # Use configured benchmark suites root or default
            benchmark_suites_root = Path(
                self.benchmark_suites_root or "benchmark-suites"
            )

            # Construct path to suite file
            suite_path = benchmark_suites_root / f"{self.benchmark_suite}.yaml"

            if not suite_path.exists():
                raise ValueError(f"Benchmark suite file not found: {suite_path}")

            # Load and validate suite file
            with suite_path.open() as f:
                suite_data = yaml.safe_load(f)

            # Validate using BenchmarkSuiteConfig schema
            suite_config = BenchmarkSuiteConfig(**suite_data)

            return suite_config.get_benchmark_names()

        # Should never reach here due to __init__ validation
        raise ValueError("No benchmark source specified")

    def get_benchmark_entries(self) -> List[BenchmarkEntry]:
        """Get the list of benchmark entries with harness information.

        Returns:
            List of BenchmarkEntry objects with name and optional harnesses

        Raises:
            ValueError: If benchmark_suite file doesn't exist or is invalid
        """
        if self.benchmarks is not None:
            # Convert benchmarks list to BenchmarkEntry objects
            entries = []
            for item in self.benchmarks:
                if isinstance(item, str):
                    entries.append(BenchmarkEntry(name=item, harnesses=None))
                elif isinstance(item, dict):
                    name = list(item.keys())[0]
                    harnesses = item[name]
                    entries.append(BenchmarkEntry(name=name, harnesses=harnesses))
            return entries

        if self.benchmark_suite is not None:
            from pathlib import Path

            import yaml

            # Use configured benchmark suites root or default
            benchmark_suites_root = Path(
                self.benchmark_suites_root or "benchmark-suites"
            )

            # Construct path to suite file
            suite_path = benchmark_suites_root / f"{self.benchmark_suite}.yaml"

            if not suite_path.exists():
                raise ValueError(f"Benchmark suite file not found: {suite_path}")

            # Load and validate suite file
            with suite_path.open() as f:
                suite_data = yaml.safe_load(f)

            # Validate using BenchmarkSuiteConfig schema
            suite_config = BenchmarkSuiteConfig(**suite_data)

            return suite_config.get_benchmark_entries()

        # Should never reach here due to __init__ validation
        raise ValueError("No benchmark source specified")


class BenchmarkSuiteConfig(BaseModel):
    """Benchmark suite configuration schema."""

    model_config = {"populate_by_name": True}  # Pydantic V2 syntax

    Name: str = Field(..., description="Unique identifier for the benchmark suite")
    Description: str = Field(
        ..., description="Description of the benchmark suite purpose and scope"
    )
    benchmark_list: List[Union[str, Dict[str, List[str]]]] = Field(
        ...,
        description="List of benchmarks. Each entry can be either a string (benchmark name) "
        "or a dict mapping benchmark name to list of harnesses",
    )

    # Note: "Release date" field name has a space, handling with Field alias
    release_date: str = Field(
        ..., alias="Release date", description="Release date of the benchmark suite"
    )

    @field_validator("Name")
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Name cannot be empty")
        return v.strip()

    @field_validator("Description")
    @classmethod
    def validate_description(cls, v):
        if not v or not v.strip():
            raise ValueError("Benchmark suite Description cannot be empty")
        return v.strip()

    @field_validator("release_date")
    @classmethod
    def validate_release_date(cls, v):
        if not v or not v.strip():
            raise ValueError("Release date cannot be empty")

        # Validate date format MM.DD.YYYY
        date_str = v.strip()
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_str):
            raise ValueError(
                f"Invalid release date format: {date_str}. Expected format: MM.DD.YYYY (e.g., 09.23.2025)"
            )

        return date_str

    @field_validator("benchmark_list")
    @classmethod
    def validate_benchmark_list(cls, v):
        if not v:
            raise ValueError("benchmark_list must contain at least one benchmark ID")

        benchmark_names = []
        cleaned_list = []

        for item in v:
            if isinstance(item, str):
                # Simple string format
                name = item.strip()
                if not name:
                    raise ValueError("benchmark_list contains empty benchmark ID")
                benchmark_names.append(name)
                cleaned_list.append(name)
            elif isinstance(item, dict):
                # Dict format: {benchmark_name: [harness_list]}
                if len(item) != 1:
                    raise ValueError(
                        "Each dict entry must have exactly one benchmark name as key"
                    )
                name = list(item.keys())[0].strip()
                harnesses = item[name]

                if not name:
                    raise ValueError("Benchmark name cannot be empty")
                if not isinstance(harnesses, list) or not harnesses:
                    raise ValueError(
                        f"Harness list for '{name}' must be a non-empty list"
                    )
                if any(not h or not str(h).strip() for h in harnesses):
                    raise ValueError(
                        f"Harness list for '{name}' contains empty harness names"
                    )

                benchmark_names.append(name)
                # Store normalized format
                cleaned_list.append({name: [str(h).strip() for h in harnesses]})
            else:
                raise ValueError(
                    f"Invalid benchmark_list entry type: {type(item).__name__}. "
                    "Expected str or dict"
                )

        # Check for duplicate benchmark names
        if len(benchmark_names) != len(set(benchmark_names)):
            duplicates = [
                name for name in benchmark_names if benchmark_names.count(name) > 1
            ]
            raise ValueError(
                f"Duplicate benchmark IDs found: {', '.join(set(duplicates))}"
            )

        return cleaned_list

    def get_benchmark_names(self) -> List[str]:
        """Get list of benchmark names only (backward compatible).

        Returns:
            List of benchmark IDs without harness information
        """
        names = []
        for item in self.benchmark_list:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(list(item.keys())[0])
        return names

    def get_benchmark_entries(self) -> List[BenchmarkEntry]:
        """Get list of benchmark entries with harness information.

        Returns:
            List of BenchmarkEntry objects with name and optional harnesses
        """
        entries = []
        for item in self.benchmark_list:
            if isinstance(item, str):
                entries.append(BenchmarkEntry(name=item, harnesses=None))
            elif isinstance(item, dict):
                name = list(item.keys())[0]
                harnesses = item[name]
                entries.append(BenchmarkEntry(name=name, harnesses=harnesses))
        return entries


class ProjectConfig(BaseModel):
    """OSS-Fuzz compatible project.yaml configuration schema.

    This schema validates project.yaml files used in CRSBench benchmarks.
    Compatible with OSS-Fuzz project configuration format with CRSBench extensions.

    Note: Uses extra="ignore" to allow unknown fields from standard OSS-Fuzz
    project.yaml files that we don't model (e.g., vendor, primary_contact).
    """

    model_config = {"extra": "ignore"}

    homepage: Optional[str] = Field(default=None, description="Project homepage URL")
    language: str = Field(
        ..., description="Programming language (c, c++, jvm, go, rust, python, etc.)"
    )
    main_repo: Optional[str] = Field(default=None, description="Main repository URL")
    sanitizers: List[str] = Field(
        default_factory=list,
        description="List of sanitizers (address, memory, undefined, thread, leak)",
    )
    fuzzing_engines: List[str] = Field(
        default_factory=list,
        description="List of fuzzing engines (libfuzzer, afl, honggfuzz)",
    )
    architectures: List[str] = Field(
        default_factory=lambda: ["x86_64"],
        description="Target architectures (default: x86_64)",
    )

    # CRSBench extensions
    rts_mode: Optional[RtsMode] = Field(
        default=None,
        description="Regression Test Selection mode (none, jcgeks, openclover, binaryrts)",
    )
    inc_build: bool = Field(
        default=True,
        description="Whether incremental build is supported",
    )

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        if not v or not v.strip():
            raise ValueError("Language cannot be empty")
        return v.strip().lower()

    @field_validator("sanitizers")
    @classmethod
    def validate_sanitizers(cls, v):
        valid_sanitizers = {"address", "memory", "undefined", "thread", "leak"}
        for sanitizer in v:
            if sanitizer not in valid_sanitizers:
                raise ValueError(
                    f"Invalid sanitizer: {sanitizer}. "
                    f"Must be one of: {', '.join(sorted(valid_sanitizers))}"
                )
        return v

    @field_validator("fuzzing_engines")
    @classmethod
    def validate_fuzzing_engines(cls, v):
        valid_engines = {"libfuzzer", "afl", "honggfuzz", "centipede", "jazzer"}
        for engine in v:
            if engine not in valid_engines:
                raise ValueError(
                    f"Invalid fuzzing engine: {engine}. "
                    f"Must be one of: {', '.join(sorted(valid_engines))}"
                )
        return v

    def get_normalized_language(self) -> str:
        """Get normalized language name for display.

        Returns:
            Normalized language (C/C++, Java, Go, Rust, Python, etc.)
        """
        lang = self.language.upper()
        if lang in ("C", "C++"):
            return "C/C++"
        if lang == "JVM":
            return "Java"
        return lang.capitalize()
