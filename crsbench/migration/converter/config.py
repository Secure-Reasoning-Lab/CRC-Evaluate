"""
Configuration file converter for bidirectional format conversion.

Supports:
- Atlanta config.yaml → RFC meta.yaml (convert)
- RFC meta.yaml → Atlanta config.yaml (convert_reverse)
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from crsbench.migration.models import (
    POV,
    DeltaMode,
    FullMode,
    HarnessFile,
    MetaConfig,
    Vulnerability,
)
from crsbench.utils.logger import get_logger


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
        with config_path.open("r") as f:
            config = yaml.safe_load(f)

        # Extract sections
        delta_mode = self._convert_delta_mode(config.get("delta_mode"))
        full_mode = self._convert_full_mode(config.get("full_mode"))
        harness_files, harness_info = self._convert_harness_files(
            config.get("harness_files", [])
        )

        # Build MetaConfig using Pydantic model
        meta_config = MetaConfig(
            patch_exclude_list=self._get_default_patch_exclusions(),
            delta_mode=delta_mode,
            full_mode=full_mode,
            harness_files=harness_files,
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
        if "base_commit" not in delta_mode or "ref_commit" not in delta_mode:
            self.logger.warning("delta_mode missing required fields")
            return None

        return DeltaMode(
            base_commit=delta_mode["base_commit"], ref_commit=delta_mode["ref_commit"]
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
        if "base_commit" not in full_mode:
            self.logger.warning("full_mode missing base_commit")
            return None

        return FullMode(base_commit=full_mode["base_commit"])

    def _convert_harness_files(
        self, harness_files: list
    ) -> Tuple[List[HarnessFile], Dict[str, Dict]]:
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
            harness_name = harness.get("name")
            harness_path = harness.get("path")

            if not harness_name or not harness_path:
                self.logger.warning(
                    f"Skipping harness with missing name or path: {harness}"
                )
                continue

            # Store CPV info for later processing
            cpvs = harness.get("cpvs", [])
            harness_info[harness_name] = {"path": harness_path, "cpvs": cpvs}

            # Convert CPVs to vulns format
            vulns = None
            if cpvs:
                vuln_list = []
                for cpv in cpvs:
                    cpv_name = cpv.get("name")
                    if not cpv_name:
                        self.logger.warning(
                            f"CPV missing name in harness {harness_name}"
                        )
                        continue

                    pov = POV(
                        id="pov_0",  # Primary POV
                        sanitizer=cpv.get("sanitizer", "address"),
                        error_token=cpv.get(
                            "error_token", "# MOCK: error token not provided"
                        ),
                    )

                    vuln = Vulnerability(vuln_keyword=cpv_name, povs=[pov])

                    vuln_list.append(vuln)

                vulns = vuln_list if vuln_list else None

            # Build HarnessFile model
            harness_model = HarnessFile(
                name=harness_name, path=harness_path, vulns=vulns
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

    # =========================================================================
    # Reverse Conversion: RFC meta.yaml → Atlanta config.yaml
    # =========================================================================

    def convert_reverse(self, meta_path: Path) -> Dict[str, Any]:
        """
        Convert RFC meta.yaml to Atlanta config.yaml format.

        Args:
            meta_path: Path to the RFC meta.yaml file

        Returns:
            Dictionary representing Atlanta config.yaml structure
        """
        with meta_path.open("r") as f:
            meta = yaml.safe_load(f)

        config: Dict[str, Any] = {}

        # Convert delta_mode (preserve as-is if present)
        if meta.get("delta_mode"):
            delta = meta["delta_mode"]
            # Atlanta format uses list for delta_mode with cpvs inside
            config["delta_mode"] = [
                {
                    "base_commit": delta.get("base_commit"),
                    "ref_commit": delta.get("ref_commit"),
                }
            ]

        # Convert full_mode (preserve as-is if present)
        if meta.get("full_mode"):
            config["full_mode"] = {"base_commit": meta["full_mode"].get("base_commit")}

        # Convert harness_files (vulns → cpvs)
        config["harness_files"] = []
        for harness in meta.get("harness_files", []):
            harness_entry: Dict[str, Any] = {
                "name": harness.get("name"),
                "path": harness.get("path"),
            }

            # Convert vulns to cpvs
            vulns = harness.get("vulns", [])
            if vulns:
                cpvs = []
                for vuln in vulns:
                    # Take first POV's sanitizer/error_token
                    povs = vuln.get("povs", [])
                    pov = povs[0] if povs else {}

                    cpv_entry: Dict[str, Any] = {
                        "name": vuln.get("vuln_keyword"),
                        "sanitizer": pov.get("sanitizer", "address"),
                    }

                    # Only include error_token if present and not a mock
                    error_token = pov.get("error_token")
                    if error_token and not error_token.startswith("# MOCK"):
                        cpv_entry["error_token"] = error_token

                    cpvs.append(cpv_entry)

                harness_entry["cpvs"] = cpvs

            config["harness_files"].append(harness_entry)

        return config

    def write_atlanta_config(self, config: Dict[str, Any], output_path: Path) -> None:
        """
        Write Atlanta config.yaml to file.

        Args:
            config: Atlanta config dictionary
            output_path: Path to write config.yaml
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w") as f:
            yaml.dump(
                config,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
                width=120,
            )
