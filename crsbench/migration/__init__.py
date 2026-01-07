"""Migration scripts for converting benchmark formats to unified YAML specification.

This module contains Python scripts and tools for migrating from legacy
benchmark formats to the new unified CRSBench YAML specification.
"""

from crsbench.migration.converter import (
    AtlantaToRFCMigrator,
    ConfigConverter,
    FileMigrator,
    RFCToAtlantaMigrator,
)
from crsbench.migration.models import (
    POV,
    DeltaMode,
    FullMode,
    HarnessFile,
    MetaConfig,
    Vulnerability,
    VulnerabilityLocation,
    VulnerabilityMetadata,
)
from crsbench.migration.test_sh import (
    ShTestGenerator,
    generate_test_sh_for_benchmark,
)
from crsbench.migration.validator import (
    MigrationValidationResult,
    ValidationCodes,
    ValidationIssue,
    validate_source_target,
)
from crsbench.migration.vuln_yaml import (
    VulnYamlGenerator,
    VulnYamlValidationError,
    generate_vuln_yaml_for_cpv,
    validate_vuln_yaml,
)

__all__ = [
    # Converter classes
    "AtlantaToRFCMigrator",
    "RFCToAtlantaMigrator",
    "ConfigConverter",
    "FileMigrator",
    # Validation
    "MigrationValidationResult",
    "ValidationCodes",
    "ValidationIssue",
    "validate_source_target",
    # Models
    "DeltaMode",
    "FullMode",
    "HarnessFile",
    "MetaConfig",
    "POV",
    "Vulnerability",
    "VulnerabilityLocation",
    "VulnerabilityMetadata",
    # test.sh generation
    "ShTestGenerator",
    "generate_test_sh_for_benchmark",
    # vuln.yaml generation
    "VulnYamlGenerator",
    "VulnYamlValidationError",
    "generate_vuln_yaml_for_cpv",
    "validate_vuln_yaml",
]
