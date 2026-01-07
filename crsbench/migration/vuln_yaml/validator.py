"""
Validation utilities for vuln.yaml files.

This module contains the VulnYamlValidationError class and validate_vuln_yaml
function for validating vuln.yaml content.
"""

from typing import List

import yaml


class VulnYamlValidationError:
    """Represents a validation error in vuln.yaml content."""

    def __init__(self, field: str, message: str, value: str = ""):
        self.field = field
        self.message = message
        self.value = value

    def __str__(self):
        if self.value:
            return f"{self.field}: {self.message} (value: '{self.value}')"
        return f"{self.field}: {self.message}"


def validate_vuln_yaml(yaml_content: str) -> List[VulnYamlValidationError]:
    """
    Validate vuln.yaml content for common issues.

    Checks for:
    - Valid YAML syntax
    - Required fields (id, name, cwes, description, locations)
    - Special characters in unquoted values (colons, etc.)
    - MOCK/TBD placeholder content

    Args:
        yaml_content: The YAML content to validate

    Returns:
        List of VulnYamlValidationError objects (empty if valid)
    """
    errors = []

    # Try to parse YAML
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        errors.append(
            VulnYamlValidationError("yaml_syntax", f"Invalid YAML syntax: {str(e)}")
        )
        return errors

    if not isinstance(data, dict):
        errors.append(
            VulnYamlValidationError(
                "structure", "YAML must be a dictionary/mapping at the root level"
            )
        )
        return errors

    # Check required fields
    required_fields = ["id", "name", "cwes", "description", "locations"]
    for field in required_fields:
        if field not in data:
            errors.append(
                VulnYamlValidationError(field, f"Missing required field: {field}")
            )

    # Validate 'name' field - check for unquoted special characters
    if "name" in data:
        name = str(data["name"])
        # Check for MOCK/TBD placeholders
        if "MOCK:" in name or "(TBD)" in name:
            errors.append(
                VulnYamlValidationError(
                    "name", "Contains placeholder text (MOCK: or TBD)", name
                )
            )
        # Check for problematic patterns that indicate parsing issues
        # If the name contains a colon and the YAML parsed it incorrectly,
        # the data structure would be wrong
        if name == "None" or name == "":
            errors.append(
                VulnYamlValidationError("name", "Name field is empty or None", name)
            )

    # Check the raw content for unquoted colons in name field
    # This catches cases where YAML might parse incorrectly
    lines = yaml_content.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("name:"):
            # Get the value part after "name:"
            value_part = stripped[5:].strip()
            # If the value contains a colon and is not quoted, it's problematic
            if ":" in value_part and not (
                (value_part.startswith('"') and value_part.endswith('"'))
                or (value_part.startswith("'") and value_part.endswith("'"))
            ):
                errors.append(
                    VulnYamlValidationError(
                        "name",
                        "Contains unquoted colon (:) which may cause YAML parsing issues. "
                        "Either remove the colon or wrap the value in quotes.",
                        value_part,
                    )
                )
            break

    # Validate 'description' field
    if "description" in data:
        desc = str(data["description"])
        if "MOCK:" in desc or "(TBD)" in desc:
            errors.append(
                VulnYamlValidationError(
                    "description",
                    "Contains placeholder text (MOCK: or TBD)",
                    desc[:100] + "..." if len(desc) > 100 else desc,
                )
            )
        if desc == "None" or desc.strip() == "":
            errors.append(
                VulnYamlValidationError(
                    "description", "Description field is empty or None"
                )
            )

    # Validate 'cwes' field
    if "cwes" in data:
        cwes = data["cwes"]
        if not isinstance(cwes, list):
            errors.append(VulnYamlValidationError("cwes", "Must be a list", str(cwes)))
        elif len(cwes) == 0:
            errors.append(VulnYamlValidationError("cwes", "CWE list is empty"))

    # Validate 'locations' field
    if "locations" in data:
        locations = data["locations"]
        if not isinstance(locations, list):
            errors.append(
                VulnYamlValidationError("locations", "Must be a list", str(locations))
            )
        elif len(locations) == 0:
            errors.append(
                VulnYamlValidationError("locations", "Locations list is empty")
            )
        else:
            for idx, loc in enumerate(locations):
                if not isinstance(loc, dict):
                    errors.append(
                        VulnYamlValidationError(
                            f"locations[{idx}]", "Each location must be a dictionary"
                        )
                    )
                    continue

                # Check required location fields
                loc_required = ["path_from_root", "function_name"]
                for field in loc_required:
                    if field not in loc:
                        errors.append(
                            VulnYamlValidationError(
                                f"locations[{idx}].{field}",
                                f"Missing required field: {field}",
                            )
                        )

                # Check for placeholder values in path_from_root
                if "path_from_root" in loc:
                    path = str(loc["path_from_root"])
                    if path in ["unknown", "None", ""]:
                        errors.append(
                            VulnYamlValidationError(
                                f"locations[{idx}].path_from_root",
                                "Contains placeholder or empty value",
                                path,
                            )
                        )

    return errors
