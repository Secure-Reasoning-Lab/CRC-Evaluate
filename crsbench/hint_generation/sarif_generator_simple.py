"""Simplified SARIF format hint generator for CRSBench.

This module generates SARIF format hints at different levels of detail
based on vulnerability information from vuln.yaml files.

This version generates SARIF JSON directly without using Pydantic models
to avoid compatibility issues with the auto-generated sarif_model.py.
"""

import json
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from crsbench.hint_generation.cwe_mapping import get_general_class


class HintLevel(IntEnum):
    """Hint levels for vulnerability information disclosure."""

    GENERAL_CLASS = 1  # General vulnerability class only
    SPECIFIC_TYPE = 2  # Specific vulnerability type
    WITH_FUNCTION = 3  # + Function-level location
    WITH_LINES = 4  # + Line range-level location
    WITH_NAME_DESC = 5  # + Vulnerability name and description


class VulnInfo:
    """Container for vulnerability information from vuln.yaml."""

    def __init__(self, vuln_data: Dict[str, Any]):
        """Initialize from parsed vuln.yaml data.

        Args:
            vuln_data: Dictionary parsed from vuln.yaml
        """
        self.id = vuln_data.get("id", "unknown")
        self.name = vuln_data.get("name", "Unknown vulnerability")
        self.cwes = vuln_data.get("cwes", [])
        self.description = vuln_data.get("description", "")
        self.locations = vuln_data.get("locations", [])

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "VulnInfo":
        """Load vulnerability info from vuln.yaml file.

        Args:
            yaml_path: Path to vuln.yaml file

        Returns:
            VulnInfo instance
        """
        with yaml_path.open("r") as f:
            data = yaml.safe_load(f)
        return cls(data)


class SarifHintGenerator:
    """Generator for SARIF format hints at different levels."""

    def __init__(self, vuln_info: VulnInfo):
        """Initialize generator with vulnerability information.

        Args:
            vuln_info: Vulnerability information
        """
        self.vuln_info = vuln_info
        self.tool_name = "CRSBench-HintGenerator"
        self.tool_version = "1.0.0"

    def generate(self, level: HintLevel) -> str:
        """Generate SARIF hint at specified level.

        Args:
            level: Hint level to generate

        Returns:
            JSON string of SARIF report
        """
        sarif = self._create_sarif_structure(level)
        return json.dumps(sarif, indent=2)

    def _create_sarif_structure(self, level: HintLevel) -> Dict[str, Any]:
        """Create SARIF structure for given hint level.

        Args:
            level: Hint level

        Returns:
            SARIF dictionary
        """
        # Create tool component with rules
        rules = self._create_rules(level)
        driver = {
            "name": self.tool_name,
            "version": self.tool_version,
            "informationUri": "https://github.com/your-org/crsbench",
            "rules": rules,
        }

        tool = {"driver": driver}

        # Create results
        results = self._create_results(level)

        # Create run
        run = {"tool": tool, "results": results}

        # Create root SARIF object
        return {
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [run],
        }

    def _create_rules(self, level: HintLevel) -> List[Dict[str, Any]]:
        """Create rule descriptors based on hint level.

        Args:
            level: Hint level

        Returns:
            List of rule dictionaries
        """
        rules = []

        # Handle empty CWE list
        if not self.vuln_info.cwes:
            rule = {
                "id": "unknown-vulnerability",
                "name": "Unknown Vulnerability",
                "shortDescription": {"text": "Unknown vulnerability detected"},
                "fullDescription": {
                    "text": "A vulnerability was detected but no CWE classification is available."
                },
            }
            return [rule]

        if level == HintLevel.GENERAL_CLASS:
            # Level 1: Create unique rules for each general class
            seen_classes = set()
            for cwe in self.vuln_info.cwes:
                general_class, _ = get_general_class(cwe)
                if general_class in seen_classes:
                    continue
                seen_classes.add(general_class)

                rule_id = general_class.lower().replace(" ", "-")
                rule = {
                    "id": rule_id,
                    "name": general_class,
                    "shortDescription": {"text": f"{general_class} detected"},
                    "fullDescription": {
                        "text": f"A {general_class.lower()} was detected in the code."
                    },
                }
                rules.append(rule)
        else:
            # Level 2+: Create rule for each CWE
            for cwe in self.vuln_info.cwes:
                _, cwe_description = get_general_class(cwe)
                rule = {
                    "id": cwe,
                    "name": f"{cwe}: {cwe_description}",
                    "shortDescription": {"text": cwe_description},
                    "fullDescription": {
                        "text": f"Specific vulnerability type: {cwe_description}"
                    },
                }
                rules.append(rule)

        return rules

    def _create_results(self, level: HintLevel) -> List[Dict[str, Any]]:
        """Create result objects based on hint level.

        Args:
            level: Hint level

        Returns:
            List of result dictionaries
        """
        results = []

        # Handle empty CWE list
        if not self.vuln_info.cwes:
            if level == HintLevel.WITH_NAME_DESC:
                message_text = (
                    f"Unknown Vulnerability\n"
                    f"Vulnerability: {self.vuln_info.name}\n"
                    f"Description: {self.vuln_info.description}"
                )
            else:
                message_text = "Unknown Vulnerability"

            result = {
                "ruleId": "unknown-vulnerability",
                "level": "warning",
                "message": {"text": message_text},
            }

            if level >= HintLevel.WITH_FUNCTION:
                location_level = (
                    HintLevel.WITH_LINES if level == HintLevel.WITH_NAME_DESC else level
                )
                locations = self._create_locations(location_level)
                if locations:
                    result["locations"] = locations

            return [result]

        # Create results based on level
        if level == HintLevel.GENERAL_CLASS:
            # Level 1: Create unique results for each general class
            seen_classes = set()
            for cwe in self.vuln_info.cwes:
                general_class, _ = get_general_class(cwe)
                if general_class in seen_classes:
                    continue
                seen_classes.add(general_class)

                rule_id = general_class.lower().replace(" ", "-")
                result = {
                    "ruleId": rule_id,
                    "level": "warning",
                    "message": {"text": general_class},
                }
                results.append(result)
        else:
            # Level 2+: Create result for each CWE
            for cwe in self.vuln_info.cwes:
                _, cwe_description = get_general_class(cwe)

                # Create message based on level
                if level == HintLevel.SPECIFIC_TYPE:
                    message_text = f"{cwe} - {cwe_description}"
                elif level == HintLevel.WITH_FUNCTION:
                    func_names = list(
                        {
                            loc.get("function_name") or "unknown"
                            for loc in self.vuln_info.locations
                        }
                    )
                    message_text = f"{cwe} - {cwe_description} in function(s): {', '.join(func_names)}"
                elif level == HintLevel.WITH_LINES:
                    location_strs = []
                    for loc in self.vuln_info.locations:
                        path = loc.get("path_from_root", "unknown")
                        if path.startswith("MOCK: "):
                            path = path[6:]
                        start = loc.get("startLine", 0)
                        end = loc.get("endLine", 0)
                        location_strs.append(f"{path}:{start}-{end}")
                    message_text = (
                        f"{cwe} - {cwe_description} at: {'; '.join(location_strs)}"
                    )
                else:  # HintLevel.WITH_NAME_DESC
                    message_text = (
                        f"{cwe} - {cwe_description}\n"
                        f"Vulnerability: {self.vuln_info.name}\n"
                        f"Description: {self.vuln_info.description}"
                    )

                result = {
                    "ruleId": cwe,
                    "level": "warning",
                    "message": {"text": message_text},
                }

                # Add locations if level >= 3 (deduplicated)
                if level >= HintLevel.WITH_FUNCTION:
                    location_level = (
                        HintLevel.WITH_LINES
                        if level == HintLevel.WITH_NAME_DESC
                        else level
                    )
                    locations = self._create_locations(location_level)
                    if locations:
                        result["locations"] = locations

                results.append(result)

        return results

    def _create_locations(self, level: HintLevel) -> Optional[List[Dict[str, Any]]]:
        """Create location objects based on hint level.

        Args:
            level: Hint level

        Returns:
            List of location dictionaries or None (deduplicated)
        """
        if level < HintLevel.WITH_FUNCTION:
            return None

        locations = []
        seen_locations = set()  # Track unique locations

        for loc_data in self.vuln_info.locations:
            path = loc_data.get("path_from_root", "unknown")
            func_name = loc_data.get("function_name") or "unknown"

            # Sanitize path: remove "MOCK: " prefix for valid URI
            if path.startswith("MOCK: "):
                path = path[6:]

            start_line = loc_data.get("startLine", 0)
            end_line = loc_data.get("endLine", 0)
            start_col = loc_data.get("startColumn", 0)
            end_col = loc_data.get("endColumn", 0)

            # Create unique identifier for this location
            if level == HintLevel.WITH_LINES and start_line > 0:
                # Use file+line+column for uniqueness at Level 4+
                loc_key = (path, func_name, start_line, end_line, start_col, end_col)
            else:
                # Use file+function for uniqueness at Level 3
                loc_key = (path, func_name)

            # Skip if we've already added this location
            if loc_key in seen_locations:
                continue
            seen_locations.add(loc_key)

            # Create physical location
            physical_location = {"artifactLocation": {"uri": path}}

            if level == HintLevel.WITH_LINES:
                # Include line range (only if valid)
                # SARIF requires line numbers >= 1, skip region if invalid
                if start_line > 0 and end_line > 0:
                    region = {"startLine": start_line, "endLine": end_line}

                    # Columns are optional, but must be >= 1 if present
                    if start_col > 0:
                        region["startColumn"] = start_col
                    if end_col > 0:
                        region["endColumn"] = end_col

                    physical_location["region"] = region

            # Create location with physical and logical information
            location = {"physicalLocation": physical_location}

            # Add logical location (function name) for Level 3+
            # Only add if function name is known (not None, not empty, not "unknown")
            if (
                level >= HintLevel.WITH_FUNCTION
                and func_name
                and func_name not in (None, "", "unknown")
            ):
                logical_locations = [
                    {
                        "name": func_name,
                        "kind": "function",
                    }
                ]
                location["logicalLocations"] = logical_locations

            locations.append(location)

        return locations if locations else None


def generate_hints_for_benchmark(
    vuln_yaml_path: Path,
    output_dir: Path,
    levels: Optional[List[HintLevel]] = None,
    *,
    skip_existing: bool = True,
) -> Dict[HintLevel, Path]:
    """Generate SARIF hints for all levels for a benchmark.

    Args:
        vuln_yaml_path: Path to vuln.yaml file
        output_dir: Directory to write SARIF files
        levels: List of hint levels to generate (default: all levels)
        skip_existing: Skip generation if hint file already exists (default: True)

    Returns:
        Dictionary mapping hint level to output file path
    """
    if levels is None:
        levels = list(HintLevel)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load vulnerability info
    vuln_info = VulnInfo.from_yaml(vuln_yaml_path)

    # Generate hints for each level
    generator = SarifHintGenerator(vuln_info)
    output_files = {}

    for level in levels:
        # Use simplified filename format: level_1.sarif instead of hint_level_1.sarif
        output_file = output_dir / f"level_{level}.sarif"

        # Skip if file already exists and skip_existing is True
        if skip_existing and output_file.exists():
            output_files[level] = output_file
            continue

        sarif_json = generator.generate(level)

        # Save to file
        with output_file.open("w") as f:
            f.write(sarif_json)

        output_files[level] = output_file

    return output_files
