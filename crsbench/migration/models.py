"""
Pydantic models for CRSBench RFC format.

These models define the structure of vulnerability metadata YAML files
and meta.yaml configuration files generated during migration from
Team-Atlanta format to RFC format.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
import yaml


class VulnerabilityLocation(BaseModel):
    """Location of vulnerable code in the repository."""

    path_from_root: str = Field(
        description="Path to the file from repository root"
    )
    function_name: Optional[str] = Field(
        default=None,
        description="Name of the function containing the vulnerability"
    )
    startLine: int = Field(
        description="Starting line number of the vulnerable code"
    )
    startColumn: int = Field(
        description="Starting column number of the vulnerable code"
    )
    endLine: int = Field(
        description="Ending line number of the vulnerable code"
    )
    endColumn: int = Field(
        description="Ending column number of the vulnerable code"
    )

    class Config:
        # Allow extra fields for forward compatibility
        extra = "allow"


class VulnerabilityMetadata(BaseModel):
    """Complete vulnerability metadata in RFC format."""

    id: str = Field(
        description="Vulnerability identifier (e.g., cpv_0, cpv_1)"
    )
    name: str = Field(
        description="Human-readable name for the vulnerability"
    )
    cwes: List[str] = Field(
        default_factory=list,
        description="List of CWE identifiers associated with this vulnerability"
    )
    description: str = Field(
        description="Detailed description of the vulnerability"
    )
    locations: List[VulnerabilityLocation] = Field(
        default_factory=list,
        description="Code locations where the vulnerability exists"
    )

    class Config:
        # Allow extra fields for forward compatibility
        extra = "allow"

    def to_yaml(self, output_path: Path) -> None:
        """
        Write vulnerability metadata to YAML file with proper formatting.

        Args:
            output_path: Path where YAML file should be written
        """
        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dictionary
        data = self.model_dump(exclude_none=False)

        # Use custom representer for better formatting
        yaml.add_representer(
            type(None),
            lambda dumper, value: dumper.represent_scalar('tag:yaml.org,2002:null', 'null')
        )

        # Dump to string first for post-processing
        yaml_str = yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            width=120
        )

        # Add line breaks between major sections for readability
        yaml_str = self._add_section_breaks(yaml_str)

        with open(output_path, 'w') as f:
            f.write(yaml_str)

    def _add_section_breaks(self, yaml_str: str) -> str:
        """
        Add line breaks between major sections for better readability.

        Args:
            yaml_str: YAML string

        Returns:
            YAML string with added line breaks
        """
        lines = yaml_str.split('\n')
        result = []
        prev_line = ''

        for line in lines:
            # Add blank line before top-level keys (except the first one)
            if line and not line.startswith(' ') and not line.startswith('-') and result and prev_line:
                if prev_line.strip():  # Don't add if previous line is already blank
                    result.append('')
            result.append(line)
            prev_line = line

        return '\n'.join(result)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VulnerabilityMetadata":
        """
        Create VulnerabilityMetadata from dictionary.

        Args:
            data: Dictionary containing vulnerability metadata

        Returns:
            VulnerabilityMetadata instance
        """
        return cls(**data)

    @classmethod
    def from_yaml(cls, input_path: Path) -> "VulnerabilityMetadata":
        """
        Load vulnerability metadata from YAML file.

        Args:
            input_path: Path to YAML file

        Returns:
            VulnerabilityMetadata instance
        """
        with open(input_path, 'r') as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)


# ============================================================================
# Meta.yaml Configuration Models
# ============================================================================

class POV(BaseModel):
    """Proof of Vulnerability variant information."""

    id: str = Field(
        description="POV variant identifier (e.g., pov_0, pov_1)"
    )
    sanitizer: str = Field(
        description="Sanitizer type (e.g., address, memory, undefined)"
    )
    error_token: str = Field(
        description="Error signature for this POV"
    )

    class Config:
        extra = "allow"


class Vulnerability(BaseModel):
    """Vulnerability information in meta.yaml."""

    vuln_keyword: str = Field(
        description="Vulnerability identifier/keyword"
    )
    povs: List[POV] = Field(
        default_factory=list,
        description="List of POV variants for this vulnerability"
    )

    class Config:
        extra = "allow"


class HarnessFile(BaseModel):
    """Harness file specification."""

    name: str = Field(
        description="Harness name"
    )
    path: str = Field(
        description="Path to harness file from repository root"
    )
    vulns: Optional[List[Vulnerability]] = Field(
        default=None,
        description="List of vulnerabilities for this harness"
    )

    class Config:
        extra = "allow"


class DeltaMode(BaseModel):
    """Delta mode configuration."""

    base_commit: str = Field(
        description="Base commit hash (non-vulnerable version)"
    )
    ref_commit: str = Field(
        description="Reference commit hash (vulnerable version)"
    )

    class Config:
        extra = "allow"


class FullMode(BaseModel):
    """Full mode configuration."""

    base_commit: str = Field(
        description="Base commit hash (vulnerable version)"
    )

    class Config:
        extra = "allow"


class MetaConfig(BaseModel):
    """Complete meta.yaml configuration in RFC format."""

    patch_exclude_list: List[str] = Field(
        default_factory=list,
        description="List of file patterns that patches cannot modify"
    )
    delta_mode: Optional[DeltaMode] = Field(
        default=None,
        description="Delta mode configuration"
    )
    full_mode: Optional[FullMode] = Field(
        default=None,
        description="Full mode configuration"
    )
    harness_files: List[HarnessFile] = Field(
        default_factory=list,
        description="List of harness file specifications"
    )

    class Config:
        extra = "allow"

    def to_yaml(self, output_path: Path) -> None:
        """
        Write meta.yaml configuration to file with proper formatting.

        Args:
            output_path: Path where meta.yaml should be written
        """
        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dictionary, excluding None values
        data = self.model_dump(exclude_none=True)

        # Use custom representer for better formatting
        yaml.add_representer(
            type(None),
            lambda dumper, value: dumper.represent_scalar('tag:yaml.org,2002:null', 'null')
        )

        # Dump to string first for post-processing
        yaml_str = yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            width=120
        )

        # Add line breaks between major sections for readability
        yaml_str = self._add_section_breaks(yaml_str)

        with open(output_path, 'w') as f:
            f.write(yaml_str)

    def _add_section_breaks(self, yaml_str: str) -> str:
        """
        Add line breaks between major sections for better readability.

        Args:
            yaml_str: YAML string

        Returns:
            YAML string with added line breaks
        """
        lines = yaml_str.split('\n')
        result = []
        prev_line = ''
        in_harness_files = False

        for i, line in enumerate(lines):
            # Track if we're in harness_files section
            if line.startswith('harness_files:'):
                in_harness_files = True
            elif line and not line.startswith(' ') and not line.startswith('-'):
                in_harness_files = False

            # Add blank line before top-level keys (major sections)
            if line and not line.startswith(' ') and not line.startswith('-') and result and prev_line:
                if prev_line.strip():
                    result.append('')
            # Add blank line before harness entries (- name:) in harness_files section only
            elif in_harness_files and line.strip().startswith('- name:') and result and prev_line:
                if prev_line.strip() and not prev_line.strip() == 'harness_files:':
                    result.append('')

            result.append(line)
            prev_line = line

        return '\n'.join(result)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetaConfig":
        """
        Create MetaConfig from dictionary.

        Args:
            data: Dictionary containing meta.yaml configuration

        Returns:
            MetaConfig instance
        """
        return cls(**data)

    @classmethod
    def from_yaml(cls, input_path: Path) -> "MetaConfig":
        """
        Load meta.yaml configuration from file.

        Args:
            input_path: Path to meta.yaml file

        Returns:
            MetaConfig instance
        """
        with open(input_path, 'r') as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)
