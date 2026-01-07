"""
vuln.yaml generation package for CRSBench benchmarks.

This package uses Claude Agent SDK to automatically generate vuln.yaml files
for CPVs (Challenge Project Vulnerabilities) that don't have them. It analyzes
crash logs, POV files, patches, and source code to generate accurate
vulnerability metadata.
"""

from crsbench.migration.vuln_yaml.generator import VulnYamlGenerator
from crsbench.migration.vuln_yaml.utils import generate_vuln_yaml_for_cpv
from crsbench.migration.vuln_yaml.validator import (
    VulnYamlValidationError,
    validate_vuln_yaml,
)

__all__ = [
    "VulnYamlGenerator",
    "VulnYamlValidationError",
    "generate_vuln_yaml_for_cpv",
    "validate_vuln_yaml",
]
