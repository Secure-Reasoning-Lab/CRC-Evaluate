"""
Configuration file converter for Team-Atlanta format to RFC format.

Converts config.yaml from Team-Atlanta format to meta.yaml in RFC format.
"""

from crsbench.utils.logger import get_logger
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from crsbench.migration.models import (
    DeltaMode,
    FullMode,
    HarnessFile,
    MetaConfig,
    POV,
    Vulnerability,
)


class ConfigConverter:
    """Converts Team-Atlanta config.yaml to RFC meta.yaml format."""

    def __init__(self):
        """Initialize the configuration converter."""
        self.logger = get_logger(__name__)

    def convert(self, config_path: Path) -> Tuple[MetaConfig, Dict[str, Dict]]:
        """
        Convert Team-Atlanta config.yaml to RFC meta.yaml format.

        Args:
            config_path: Path to the source config.yaml file

        Returns:
            Tuple of (meta_config, harness_info_dict)
            - meta_config: MetaConfig Pydantic model instance
            - harness_info_dict: Dictionary mapping harness names to their CPV info
        """
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Extract sections
        delta_mode = self._convert_delta_mode(config.get('delta_mode'))
        full_mode = self._convert_full_mode(config.get('full_mode'))
        harness_files, harness_info = self._convert_harness_files(config.get('harness_files', []))

        # Build MetaConfig using Pydantic model
        meta_config = MetaConfig(
            patch_exclude_list=self._get_default_patch_exclusions(),
            delta_mode=delta_mode,
            full_mode=full_mode,
            harness_files=harness_files
        )

        return meta_config, harness_info

    def _convert_delta_mode(self, delta_mode: Any) -> Optional[DeltaMode]:
        """
        Convert delta_mode section.

        Team-Atlanta format supports both single dict and list of dicts.
        RFC format uses a single dict.

        Args:
            delta_mode: Delta mode configuration from Team-Atlanta

        Returns:
            DeltaMode model instance, or None if not present
        """
        if not delta_mode:
            return None

        # Handle list format (take first entry)
        if isinstance(delta_mode, list):
            if len(delta_mode) == 0:
                return None
            delta_mode = delta_mode[0]

        # Validate required fields
        if 'base_commit' not in delta_mode or 'ref_commit' not in delta_mode:
            self.logger.warning("delta_mode missing required fields")
            return None

        return DeltaMode(
            base_commit=delta_mode['base_commit'],
            ref_commit=delta_mode['ref_commit']
        )

    def _convert_full_mode(self, full_mode: Any) -> Optional[FullMode]:
        """
        Convert full_mode section.

        Args:
            full_mode: Full mode configuration from Team-Atlanta

        Returns:
            FullMode model instance, or None if not present
        """
        if not full_mode:
            return None

        # Validate required fields
        if 'base_commit' not in full_mode:
            self.logger.warning("full_mode missing base_commit")
            return None

        return FullMode(
            base_commit=full_mode['base_commit']
        )

    def _convert_harness_files(self, harness_files: list) -> Tuple[List[HarnessFile], Dict[str, Dict]]:
        """
        Convert harness_files section.

        Transforms Team-Atlanta format with CPVs to RFC format with vulns.

        Args:
            harness_files: List of harness file configurations

        Returns:
            Tuple of (converted_harness_models, harness_info_dict)
        """
        converted_harnesses = []
        harness_info = {}

        for harness in harness_files:
            harness_name = harness.get('name')
            harness_path = harness.get('path')

            if not harness_name or not harness_path:
                self.logger.warning(f"Skipping harness with missing name or path: {harness}")
                continue

            # Store CPV info for later processing
            cpvs = harness.get('cpvs', [])
            harness_info[harness_name] = {
                'path': harness_path,
                'cpvs': cpvs
            }

            # Convert CPVs to vulns format
            vulns = None
            if cpvs:
                vuln_list = []
                for cpv in cpvs:
                    cpv_name = cpv.get('name')
                    if not cpv_name:
                        self.logger.warning(f"CPV missing name in harness {harness_name}")
                        continue

                    pov = POV(
                        id='pov_0',  # Primary POV
                        sanitizer=cpv.get('sanitizer', 'address'),
                        error_token=cpv.get('error_token', '# MOCK: error token not provided')
                    )

                    vuln = Vulnerability(
                        vuln_keyword=cpv_name,
                        povs=[pov]
                    )

                    vuln_list.append(vuln)

                vulns = vuln_list if vuln_list else None

            # Build HarnessFile model
            harness_model = HarnessFile(
                name=harness_name,
                path=harness_path,
                vulns=vulns
            )

            converted_harnesses.append(harness_model)

        return converted_harnesses, harness_info

    def _get_default_patch_exclusions(self) -> list:
        """
        Get default patch exclusion list for RFC format.

        Returns:
            List of file patterns that patches cannot modify
        """
        return [
            "build.sh",
            "Makefile",
            "CMakeLists.txt",
            "configure*",
            "*.ac",
            "*.am",
            ".gitignore",
            ".aixcc/**",
            "**/*test*.c",
            "**/*test*.cc",
            "**/*test*.cpp",
            "**/*test*.h",
            "test/**",
            "tests/**",
            "docs/**",
            "*.md",
            "README*",
        ]
