"""CRS helper utilities."""

from pathlib import Path

import yaml


def get_all_crs_registry_names(
    crs_config_name: str, crs_configs_dir: Path
) -> list[str]:
    """Extract ALL CRS registry names from config-resource.yaml.

    A single config file can reference multiple CRS entries in the registry.
    This function returns all of them.

    Args:
        crs_config_name: Name of the CRS config (e.g., 'atlantis-multilang-dind_given_fuzzer')
        crs_configs_dir: Path to CRS configs directory

    Returns:
        List of registry names (e.g., ['atlantis-multilang-dind', 'atlantis-multilang'])

    Raises:
        FileNotFoundError: If config-resource.yaml not found
        ValueError: If crs section is missing

    Example:
        >>> get_all_crs_registry_names('ensemble-config', Path('/configs'))
        ['atlantis-multilang-dind', 'atlantis-multilang', 'some-other-crs']
    """
    config_resource_path = crs_configs_dir / crs_config_name / "config-resource.yaml"

    if not config_resource_path.exists():
        raise FileNotFoundError(f"CRS config file not found: {config_resource_path}")

    with config_resource_path.open() as f:
        config_data = yaml.safe_load(f)

    crs_section = config_data.get("crs", {})
    if not crs_section:
        raise ValueError(f"No 'crs' section in {config_resource_path}")

    # Return ALL CRS registry names
    return list(crs_section.keys())


def get_crs_registry_name(crs_config_name: str, crs_configs_dir: Path) -> str:
    """Extract CRS registry name from config-resource.yaml.

    Returns the first CRS registry name. For configs with multiple CRS entries,
    use get_all_crs_registry_names() instead.

    Args:
        crs_config_name: Name of the CRS config (e.g., 'atlantis-multilang-dind_given_fuzzer')
        crs_configs_dir: Path to CRS configs directory

    Returns:
        Registry name (e.g., 'atlantis-multilang-dind')

    Raises:
        FileNotFoundError: If config-resource.yaml not found
        ValueError: If crs section is missing
    """
    # Delegate to get_all and return first
    all_names = get_all_crs_registry_names(crs_config_name, crs_configs_dir)
    return all_names[0]


def get_crs_worker_resources(
    crs_config_name: str, crs_configs_dir: Path
) -> dict[str, str | None]:
    """Get worker resources (cpuset, memory) from config-resource.yaml.

    Reads the first worker's resource configuration from the config file.

    Args:
        crs_config_name: Name of the CRS config
        crs_configs_dir: Path to CRS configs directory

    Returns:
        Dict with 'cpuset' and 'memory' keys (values may be None)
        Example: {"cpuset": "0-15", "memory": "16G"}

    Raises:
        FileNotFoundError: If config-resource.yaml not found
    """
    config_resource_path = crs_configs_dir / crs_config_name / "config-resource.yaml"

    if not config_resource_path.exists():
        raise FileNotFoundError(f"CRS config file not found: {config_resource_path}")

    with config_resource_path.open() as f:
        config_data = yaml.safe_load(f)

    workers = config_data.get("workers", {})
    if not workers:
        return {"cpuset": None, "memory": None}

    # Use first worker's resources
    first_worker = next(iter(workers.values()))
    return {
        "cpuset": first_worker.get("cpuset"),
        "memory": first_worker.get("memory"),
    }
