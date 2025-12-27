"""CRS helper utilities."""

from pathlib import Path

import yaml


def get_crs_registry_name(crs_config_name: str, crs_configs_dir: Path) -> str:
    """Extract CRS registry name from config-resource.yaml.

    Args:
        crs_config_name: Name of the CRS config (e.g., 'atlantis-multilang-dind_given_fuzzer')
        crs_configs_dir: Path to CRS configs directory

    Returns:
        Registry name (e.g., 'atlantis-multilang-dind')

    Raises:
        FileNotFoundError: If config-resource.yaml not found
        ValueError: If crs section is missing
    """
    config_resource_path = crs_configs_dir / crs_config_name / "config-resource.yaml"

    if not config_resource_path.exists():
        raise FileNotFoundError(f"CRS config file not found: {config_resource_path}")

    with config_resource_path.open() as f:
        config_data = yaml.safe_load(f)

    crs_section = config_data.get("crs", {})
    if not crs_section:
        raise ValueError(f"No 'crs' section in {config_resource_path}")

    # Return the first CRS registry name
    return next(iter(crs_section.keys()))
