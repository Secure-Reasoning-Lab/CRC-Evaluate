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

from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)

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
    build_time: float | None = Field(
        default=None, description="CRS build time in seconds (written after trial)"
    )
    run_time: float | None = Field(
        default=None, description="CRS run time in seconds (written after trial)"
    )

    # Worker identification (for distributed execution)
    worker_machine: Optional[str] = None
    worker_trial_dir: Optional[str] = None

    # Build configuration
    build_mode: Optional[str] = None  # "delta" or "full" (evaluation mode)
    sanitizer: Optional[str] = None
    target_cpv_id: Optional[str] = None

    # Experiment-level fields
    experiment_name: Optional[str] = None
    litellm_budget: Optional[float] = None
    cores_per_trial: Optional[int] = None
    memory_per_trial: Optional[str] = None
    experiment_config: Optional[dict[str, Any]] = None  # Full serialized config


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
    AUTO = "auto"


class Sanitizer(str, Enum):
    """Sanitizer type for fuzzing builds."""

    ADDRESS = "address"
    MEMORY = "memory"
    UNDEFINED = "undefined"


class RtsMode(str, Enum):
    """Regression Test Selection mode for Java projects."""

    NONE = "none"  # Explicitly disabled (prefer null in YAML)
    JCGEKS = "jcgeks"
    OPENCLOVER = "openclover"
    BINARYRTS = "binaryrts"


class AdapterType(str, Enum):
    """CRS adapter type.

    Single value representing the unified oss-crs adapter.
    The mode (bug-finding vs bug-fixing) is specified separately on
    the adapter instance.
    """

    OSS_CRS = "oss-crs"


@dataclass
class BenchmarkEntry:
    """Represents a benchmark with optional harness specification.

    Attributes:
        name: Benchmark identifier (e.g., afc-curl-delta-01)
        harnesses: Optional list of specific harnesses to run.
                   If None, all harnesses for this benchmark will be used.
        harness_cpvs: Optional mapping of harness -> allowed CPV IDs.
                      If provided, only these CPVs are considered for that harness.
    """

    name: str
    harnesses: Optional[List[str]] = None
    harness_cpvs: Optional[Dict[str, List[str]]] = None


@dataclass
class BenchmarkHarness:
    """A resolved benchmark-harness pair for trial execution.

    This represents a single harness within a benchmark, ready for trial execution.
    Created by resolve_benchmark_harnesses() after validating harness exists.

    Attributes:
        name: Benchmark name (e.g., "afc-curl-delta-01")
        path: Path to benchmark directory
        harness: Parsed harness with name and path
        target_cpvs: Optional subset of CPV IDs to target for this harness
    """

    name: str
    path: Path
    harness: "HarnessFile"  # Forward reference - HarnessFile defined later in this file
    target_cpvs: Optional[List[str]] = None


def _normalize_harness_list(
    benchmark_name: str, harnesses: Any, field_name: str
) -> List[str]:
    if not isinstance(harnesses, list) or not harnesses:
        raise ValueError(
            f"Harness list for '{benchmark_name}' in '{field_name}' must be a non-empty list"
        )

    normalized = [str(h).strip() for h in harnesses]
    if any(not h for h in normalized):
        raise ValueError(
            f"Harness list for '{benchmark_name}' in '{field_name}' contains empty harness names"
        )
    return normalized


def _normalize_harness_cpv_map(
    benchmark_name: str, harness_cpvs: Any, field_name: str
) -> Dict[str, List[str]]:
    if not isinstance(harness_cpvs, dict) or not harness_cpvs:
        raise ValueError(
            f"Harness/CPV map for '{benchmark_name}' in '{field_name}' must be a non-empty map"
        )

    normalized: Dict[str, List[str]] = {}
    for harness_name, cpvs in harness_cpvs.items():
        harness = str(harness_name).strip()
        if not harness:
            raise ValueError(
                f"Harness/CPV map for '{benchmark_name}' in '{field_name}' contains an empty harness name"
            )
        if not isinstance(cpvs, list) or not cpvs:
            raise ValueError(
                f"CPV list for '{benchmark_name}/{harness}' in '{field_name}' must be a non-empty list"
            )
        normalized_cpvs = [str(cpv).strip() for cpv in cpvs]
        if any(not cpv for cpv in normalized_cpvs):
            raise ValueError(
                f"CPV list for '{benchmark_name}/{harness}' in '{field_name}' contains empty CPV IDs"
            )
        normalized[harness] = normalized_cpvs
    return normalized


def _normalize_benchmark_selector_list(
    values: Any, field_name: str
) -> Optional[List[Union[str, Dict[str, Union[List[str], Dict[str, List[str]]]]]]]:
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")

    cleaned_list: List[
        Union[str, Dict[str, Union[List[str], Dict[str, List[str]]]]]
    ] = []
    benchmark_names: List[str] = []

    for item in values:
        if isinstance(item, str):
            name = item.strip()
            if not name:
                raise ValueError(f"{field_name} list contains empty benchmark ID")
            benchmark_names.append(name)
            cleaned_list.append(name)
            continue

        if isinstance(item, dict):
            if len(item) != 1:
                raise ValueError(
                    f"Each dict entry in '{field_name}' must have exactly one benchmark name as key"
                )
            raw_name = list(item.keys())[0]
            name = str(raw_name).strip()
            selector = item[raw_name]
            if not name:
                raise ValueError(f"Benchmark name in '{field_name}' cannot be empty")

            benchmark_names.append(name)

            if isinstance(selector, list):
                cleaned_list.append(
                    {name: _normalize_harness_list(name, selector, field_name)}
                )
                continue

            if isinstance(selector, dict):
                cleaned_list.append(
                    {name: _normalize_harness_cpv_map(name, selector, field_name)}
                )
                continue

            raise ValueError(
                f"Invalid benchmark selector for '{name}' in '{field_name}'. "
                "Expected list[harness] or map[harness -> list[cpv]]"
            )

        raise ValueError(
            f"Invalid {field_name} entry type: {type(item).__name__}. Expected str or dict"
        )

    if len(benchmark_names) != len(set(benchmark_names)):
        duplicates = [
            name for name in benchmark_names if benchmark_names.count(name) > 1
        ]
        raise ValueError(f"Duplicate benchmark IDs found: {', '.join(set(duplicates))}")

    return cleaned_list if cleaned_list else None


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
    patch_superset: Optional[str] = Field(
        default=None,
        description="CPV whose patch is a superset of this CPV's patch (e.g., 'cpv_7'). "
        "When specified, this CPV's patch will be skipped when applying all patches, "
        "as the superset patch already includes the fix.",
    )

    @field_validator("vuln_keyword")
    @classmethod
    def validate_vuln_keyword(cls, v):
        if not v or not v.strip():
            raise ValueError("Vulnerability keyword cannot be empty")
        v = v.strip()
        # Enforce cpv_N pattern (e.g., cpv_0, cpv_1, cpv_123)
        if not re.match(r"^cpv_\d+$", v):
            raise ValueError(
                f"Invalid vuln_keyword format: '{v}'. "
                "Must follow pattern 'cpv_N' (e.g., cpv_0, cpv_1)"
            )
        return v

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

    @field_validator("patch_superset")
    @classmethod
    def validate_patch_superset(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # Enforce cpv_N pattern
        if not re.match(r"^cpv_\d+$", v):
            raise ValueError(
                f"Invalid patch_superset format: '{v}'. "
                "Must follow pattern 'cpv_N' (e.g., cpv_0, cpv_7)"
            )
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
    """Full mode configuration with single base commit.

    In full mode, base_commit IS the vulnerable state. No ref_commit needed
    because there's no "before vulnerability" state to compare against.
    """

    base_commit: str = Field(..., description="Base commit hash (vulnerable state)")

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

    model_config = ConfigDict(extra="forbid")

    max_concurrent_requests: int = Field(
        default=10, ge=1, description="Maximum concurrent requests to LiteLLM"
    )
    cost_budget: float = Field(default=100.0, ge=0, description="Cost budget in USD")
    team: Optional[str] = Field(
        default=None,
        description="Team name to group API keys. If not set, experiment name is used.",
    )
    team_max_budget: Optional[float] = Field(
        default=None,
        ge=0,
        description="Maximum budget in USD for the team. Applied when creating or updating team.",
    )


class ResourceConfig(BaseModel):
    """Resource allocation configuration for trials."""

    model_config = ConfigDict(extra="forbid")

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
    """Distributed worker configuration.

    In distributed mode, the orchestrator serializes the full ExperimentConfig
    into each Redis job. Workers deserialize it, but paths may not match the
    worker's filesystem. Fields below (oss_fuzz_path, benchmarks_root, etc.)
    let workers override the orchestrator's paths.

    Note: All workers sharing the same config get the same overrides.
    For heterogeneous clusters, use shared storage with consistent mount points.
    """

    model_config = ConfigDict(extra="forbid")

    jobs: int = Field(default=4, ge=1, description="Number of parallel jobs per worker")
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server hostname or IP for workers. "
        "Falls back to top-level redis_host if not set.",
    )
    continuous: bool = Field(
        default=False,
        description="Whether workers should run continuously or exit after one job",
    )
    worker_name: Optional[str] = Field(
        default=None,
        description="Optional base worker name for identification. "
        "Defaults to hostname when not set.",
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
    build_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Override build_workers for workers on different machines",
    )
    verify_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Override verify_workers for workers on different machines",
    )
    benchmarks_root: Optional[Path] = Field(
        default=None,
        description="Override benchmarks root directory for workers on different machines",
    )
    benchmark_suites_root: Optional[Path] = Field(
        default=None,
        description="Override benchmark suites root directory for workers on different machines",
    )
    keep_only_results: Optional[bool] = Field(
        default=None,
        description="Override cleanup after experiment completion for workers",
    )
    cleanup_after_trial: Optional[bool] = Field(
        default=None,
        description="Override cleanup after each trial for workers",
    )
    copy_results_after_trial: Optional[bool] = Field(
        default=None,
        description="Override per-trial result copying for workers",
    )
    results_filestore: Optional[Path] = Field(
        default=None,
        description="Override results destination path for workers (contains experiment-data/ and report-data/)",
    )
    minimum_disk_size: str = Field(
        default="10GB",
        description="Minimum free disk space required to accept new jobs (e.g., '200GB', '100MB')",
    )
    disk_check_interval: int = Field(
        default=60,
        ge=1,
        description="Interval (seconds) between disk space checks when paused",
    )
    skip_cpus: Optional[str] = Field(
        default=None,
        description="CPUs to exclude from allocation (cpuset format, e.g., '0-3,8-11'). "
        "These cores are skipped when building the CPU pool.",
    )
    shared_cpus: Optional[str] = Field(
        default=None,
        description="CPUs shared across all trials without exclusive allocation (cpuset format, e.g., '0-1'). "
        "These cores are available to every container but not managed by the CPU pool.",
    )
    cores: Optional[str] = Field(
        default=None,
        description="Restrict CPU pool to these cores only (cpuset format, e.g., '16-47'). "
        "If not set, all system cores are used. Accepts cpuset format.",
    )


class CrsComposeConfig(BaseModel):
    """Configuration for oss-crs adapter."""

    model_config = ConfigDict(extra="forbid")

    docker_registry: str = Field(
        ...,
        description="Docker registry for CRS images (e.g., ghcr.io/team-atlanta/test)",
    )
    oss_crs_infra_cpuset: str = Field(
        default="0-3",
        description="CPU set for oss-crs infrastructure services",
    )
    oss_crs_infra_memory: str = Field(
        default="8G",
        description="Memory limit for oss-crs infrastructure services",
    )
    oss_crs_cmd: str = Field(
        default="oss-crs",
        description="Path to oss-crs executable (default: assumes on PATH)",
    )
    work_dir: Optional[Path] = Field(
        default=None,
        description="Base work directory for oss-crs (default: trial_output_dir/oss-crs-workdir)",
    )
    litellm_config_path: Optional[Path] = Field(
        default=None,
        description="Deprecated LiteLLM config path override. Ignored for litellm_mode='external'.",
    )


class CrsOverrideConfig(BaseModel):
    """Per-CRS runtime override configuration."""

    model_config = ConfigDict(extra="forbid")

    litellm_config_path: Optional[Path] = Field(
        default=None,
        description="Deprecated per-CRS LiteLLM config path override. Ignored for litellm_mode='external'.",
    )
    additional_env: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional environment variables injected into CRS runtime container",
    )


class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(
        default=None,
        description="Human-readable description of this experiment run",
    )
    git_hash: Optional[str] = Field(
        default=None,
        description="Git commit hash for reproducibility tracking",
    )
    experiment: str = Field(
        ..., description="Unique identifier for this experiment run"
    )
    trials: int = Field(..., ge=1, description="Number of trials (must be >= 1)")
    mode: EvaluationMode = Field(
        ...,
        description="Evaluation mode: 'delta', 'full', 'all' (run all available), or 'auto' (single mode, delta preferred)",
    )
    adapter: AdapterType = Field(
        default=AdapterType.OSS_CRS,
        description="CRS adapter type (only oss-crs supported)",
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
    sanitizers: List[Sanitizer] = Field(
        default=[Sanitizer.ADDRESS],
        description="List of sanitizers to use (address, memory, undefined). Each creates separate trials.",
    )
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server hostname or IP (optional, omit or set to 'none' for local mode)",
    )
    benchmarks_root: Path = Field(
        default=Path("benchmarks"),
        description="Root directory containing benchmark projects (default: benchmarks)",
    )
    benchmarks: Optional[
        List[Union[str, Dict[str, Union[List[str], Dict[str, List[str]]]]]]
    ] = Field(
        default=None,
        description="List of benchmarks to evaluate (mutually exclusive with benchmark_suite). "
        "Each entry can be either a string (benchmark name, runs all harnesses) "
        "or a dict mapping benchmark name to list of specific harnesses to run.",
    )
    benchmark_suite: Optional[str] = Field(
        default=None,
        description="Benchmark suite name to load (mutually exclusive with benchmarks)",
    )
    benchmark_suites_root: Path = Field(
        default=Path("benchmark-suites"),
        description="Root directory containing benchmark suite YAML files (default: benchmark-suites)",
    )
    snapshot_period: Optional[int] = Field(
        default=900,
        ge=0,
        description="Snapshot interval in seconds (0 to disable, default 900 = 15 minutes)",
    )
    registry_dir: Path = Field(
        default=Path("oss-crs/registry"),
        description="Path to CRS registry directory (default: oss-crs/registry)",
    )
    crs_configs_dir: Path = Field(
        default=Path("crses/configs"),
        description="Path to CRS configs directory (default: crses/configs)",
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
    litellm_mode: Optional[Literal["external", "self_hosted"]] = Field(
        default="external",
        description="LiteLLM mode: 'external' uses an external LiteLLM endpoint "
        "(CRSBENCH_LLM_UPSTREAM_BASE_URL/CRSBENCH_LLM_BASE_URL + CRSBENCH_LLM_UPSTREAM_API_KEY or CRSBENCH_LLM_MASTER_KEY), "
        "'self_hosted' is reserved and not implemented yet, "
        "null skips LiteLLM entirely (for CRS that don't need LLM). "
        "Default is 'external'.",
    )
    skip_litellm: bool = Field(
        default=False,
        description="Skip LiteLLM/Postgres deployment entirely inside oss-crs containers. "
        "Use when CRS does not need LLM access (e.g., pure fuzzer). "
        "Passed as --skip-litellm to oss-bugfind-crs.",
    )
    llm_tracking_enabled: bool = Field(
        default=True,
        description="Enable LLM usage tracking via LiteLLM Virtual Keys. "
        "Requires canonical runtime envs (CRSBENCH_LLM_*). "
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
    source_mode: Literal["main_repo", "pkgs"] = Field(
        default="pkgs",
        description="Source mode: 'pkgs' uses bundled tarballs from pkgs/ directory (default), "
        "'main_repo' clones from git.",
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
    per_pov_verify_timeout: int = Field(
        default=180,
        ge=1,
        description="Timeout in seconds for each single POV verification (default: 180 = 3 minutes). "
        "This is the time allowed to run the harness binary with a POV input to check if it crashes.",
    )
    max_pov_variants_per_cpv: Optional[int] = Field(
        default=1,
        ge=1,
        description="For bug-fixing CRS input staging, maximum POV variants per CPV to provide via --pov-dir. "
        "Set to 1 for a single POV per CPV (default), N for multiple variants per CPV, "
        "or null to include all available variants.",
    )
    build_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of parallel workers for building variants (default: 4).",
    )
    verify_workers: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of parallel workers for POV/patch verification (default: 4).",
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
    crs_compose: Optional[CrsComposeConfig] = Field(
        default=None,
        description="Configuration for oss-crs adapter (used at runtime by OssCrsAdapter)",
    )
    crs_overrides: Optional[Dict[str, CrsOverrideConfig]] = Field(
        default=None,
        description="Per-CRS runtime overrides keyed by CRS config name",
    )
    keep_only_results: bool = Field(
        default=False,
        description="Delete bulky artifacts after experiment, keeping only essential files for reporting",
    )
    cleanup_after_trial: bool = Field(
        default=False,
        description="Delete bulky artifacts after each trial completes",
    )
    copy_results_after_trial: bool = Field(
        default=False,
        description="Copy essential files to results_filestore after each trial completes",
    )
    results_filestore: Optional[Path] = Field(
        default=None,
        description="Destination path for copying results (contains experiment-data/ and report-data/)",
    )
    pov_dedup_strategy: Literal[
        "patch-based", "stack-based", "status-based", "none"
    ] = Field(
        default="patch-based",
        description="POV deduplication strategy: 'patch-based' (by CPV match set), "
        "'stack-based' (by crash signature from sanitizer stack trace), "
        "'status-based' (one per status type), 'none' (keep all).",
    )
    seed_corpus_enabled: bool = Field(
        default=False,
        description="Enable seed corpus from previous experiment runs. "
        "Requires corpus to be imported via 'crsbench benchmark seed-import'.",
    )
    seed_corpus_max_time: Optional[int] = Field(
        default=None,
        ge=1,
        description="Maximum relative time (seconds) for seed corpus files. "
        "Only files discovered within this time are used. None uses all available seeds.",
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

    @field_validator("sanitizers")
    @classmethod
    def validate_sanitizers(cls, v):
        """Validate sanitizers list."""
        if not v:
            raise ValueError("sanitizers list cannot be empty")
        return v

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
        return v

    @field_validator("benchmark_suites_root")
    @classmethod
    def validate_benchmark_suites_root(cls, v):
        """Validate benchmark suites root directory."""
        return v

    @field_validator("benchmarks")
    @classmethod
    def validate_benchmarks(cls, v):
        """Validate benchmarks list (string, harness list, or harness->cpv map)."""
        return _normalize_benchmark_selector_list(v, "benchmarks")

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
        if v is not None and v not in ("external", "self_hosted"):
            raise ValueError(
                f"Invalid litellm_mode: {v}. Must be 'external' or 'self_hosted'"
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
    def check_single_crs_execution_profile(self):
        """Enforce single CRS setup profile per experiment."""
        if len(self.crses) != 1:
            raise ValueError(
                f"Experiment must define exactly 1 CRS in 'crses' for a single setup profile; got {len(self.crses)}"
            )
        return self

    @model_validator(mode="after")
    def check_crs_override_keys(self):
        """Validate that crs_overrides keys match configured CRS names."""
        if not self.crs_overrides:
            return self

        valid = set(self.crses)
        unknown = sorted(k for k in self.crs_overrides.keys() if k not in valid)
        if unknown:
            raise ValueError(
                f"Unknown CRS override key(s): {', '.join(unknown)}. Valid CRS names: {', '.join(sorted(valid))}"
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

    @model_validator(mode="after")
    def check_results_filestore_configuration(self):
        """Validate results_filestore configuration."""
        if self.copy_results_after_trial and not self.results_filestore:
            raise ValueError(
                "copy_results_after_trial=true requires results_filestore to be set"
            )
        return self

    @model_validator(mode="after")
    def check_skip_litellm_overrides(self):
        """When skip_litellm is true, disable litellm_mode and tracking."""
        if self.skip_litellm:
            self.litellm_mode = None
            self.llm_tracking_enabled = False
        return self

    @model_validator(mode="after")
    def check_litellm_runtime_contract(self):
        """Fail fast when mode-required LiteLLM runtime env inputs are missing."""
        if self.skip_litellm or self.litellm_mode is None:
            return self

        if self.litellm_mode == "self_hosted":
            raise ValueError(
                "litellm_mode='self_hosted' is not implemented yet. "
                "Use litellm_mode='external'."
            )

        if (
            "litellm_mode" not in self.model_fields_set
            and "llm_tracking_enabled" not in self.model_fields_set
        ):
            return self

        runtime_env = resolve_litellm_runtime_env(self.litellm_mode)
        errors = required_env_errors_for_mode(
            runtime_env, tracking_enabled=self.llm_tracking_enabled
        )
        if errors:
            joined = "; ".join(errors)
            raise ValueError(
                f"Missing required LiteLLM runtime inputs for litellm_mode='{self.litellm_mode}': {joined}"
            )
        return self

    @model_validator(mode="after")
    def resolve_path_fields(self):
        """Resolve relative Path fields to absolute paths.

        Config YAML often specifies relative paths (e.g., ``benchmarks_root: benchmarks``).
        These must be resolved to absolute paths so that downstream code (CRS executors,
        build commands) can use them regardless of CWD changes.
        """
        path_fields = [
            "experiment_filestore",
            "report_filestore",
            "benchmarks_root",
            "benchmark_suites_root",
            "registry_dir",
            "crs_configs_dir",
            "oss_fuzz_path",
        ]
        for field_name in path_fields:
            value = getattr(self, field_name, None)
            if isinstance(value, Path) and not value.is_absolute():
                setattr(self, field_name, value.resolve())

        # Optional path field
        if self.results_filestore and not self.results_filestore.is_absolute():
            self.results_filestore = self.results_filestore.resolve()

        # Compose-level optional path field
        if (
            self.crs_compose
            and self.crs_compose.litellm_config_path
            and not self.crs_compose.litellm_config_path.is_absolute()
        ):
            self.crs_compose.litellm_config_path = (
                self.crs_compose.litellm_config_path.resolve()
            )

        # Per-CRS override optional path fields
        if self.crs_overrides:
            for override in self.crs_overrides.values():
                if (
                    override.litellm_config_path
                    and not override.litellm_config_path.is_absolute()
                ):
                    override.litellm_config_path = (
                        override.litellm_config_path.resolve()
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
                    selector = item[name]
                    if isinstance(selector, list):
                        entries.append(BenchmarkEntry(name=name, harnesses=selector))
                    else:
                        entries.append(
                            BenchmarkEntry(
                                name=name,
                                harnesses=list(selector.keys()),
                                harness_cpvs=selector,
                            )
                        )
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

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    Name: str = Field(..., description="Unique identifier for the benchmark suite")
    Description: str = Field(
        ..., description="Description of the benchmark suite purpose and scope"
    )
    benchmark_list: List[
        Union[str, Dict[str, Union[List[str], Dict[str, List[str]]]]]
    ] = Field(
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
        return _normalize_benchmark_selector_list(v, "benchmark_list")

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
                selector = item[name]
                if isinstance(selector, list):
                    entries.append(BenchmarkEntry(name=name, harnesses=selector))
                else:
                    entries.append(
                        BenchmarkEntry(
                            name=name,
                            harnesses=list(selector.keys()),
                            harness_cpvs=selector,
                        )
                    )
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
