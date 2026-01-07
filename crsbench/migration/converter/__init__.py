"""Bidirectional format converters for benchmark formats.

Supports:
- Atlanta OSS-Fuzz → RFC (AtlantaToRFCMigrator)
- RFC → Atlanta OSS-Fuzz (RFCToAtlantaMigrator)
"""

from crsbench.migration.converter.atlanta_to_rfc import AtlantaToRFCMigrator
from crsbench.migration.converter.config import ConfigConverter
from crsbench.migration.converter.files import FileMigrator
from crsbench.migration.converter.rfc_to_atlanta import RFCToAtlantaMigrator
from crsbench.migration.converter.vuln_metadata import VulnMetadataGenerator

__all__ = [
    "AtlantaToRFCMigrator",
    "RFCToAtlantaMigrator",
    "ConfigConverter",
    "FileMigrator",
    "VulnMetadataGenerator",
]
