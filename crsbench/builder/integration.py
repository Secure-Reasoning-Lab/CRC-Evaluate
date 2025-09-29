"""Integration layer for CRSBench builders.

This module provides integration between the builder system and existing CRSBench
modules like reproducer and patch_tester. It handles configuration parsing,
builder creation, and format conversion between different CRSBench components.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Type
import yaml

from crsbench.builder.base import Builder, Language, Sanitizer
from crsbench.builder.ossfuzz import OSSFuzzBuilder
from crsbench.builder.poc import POC, create_poc_from_file, convert_crsbench_pov_to_poc
from crsbench.builder.utils import check_docker_available
from crsbench.validation.schemas import POV, HarnessFile

logger = logging.getLogger(__name__)


class BuilderIntegrationError(Exception):
    """Exception raised when builder integration fails."""
    pass


def detect_project_type(benchmark_path: Path) -> str:
    """Detect the type of benchmark project.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Project type string ('ossfuzz', 'standard', 'unknown')
    """
    # Check for OSS-Fuzz indicators
    if (benchmark_path / "project.yaml").exists():
        # Check if it has OSS-Fuzz specific fields
        try:
            with open(benchmark_path / "project.yaml") as f:
                config = yaml.safe_load(f)

            # OSS-Fuzz projects typically have these fields
            ossfuzz_indicators = ["fuzzing_engines", "sanitizers", "main_repo"]
            if any(field in config for field in ossfuzz_indicators):
                return "ossfuzz"
        except Exception:
            pass

    # Check for .aixcc directory (CRSBench format)
    if (benchmark_path / ".aixcc").exists():
        return "standard"

    return "unknown"


def get_ossfuzz_path() -> Optional[Path]:
    """Get OSS-Fuzz repository path from environment or common locations.

    Returns:
        Path to OSS-Fuzz repository or None if not found
    """
    import os

    # Check environment variable
    ossfuzz_env = os.environ.get("OSS_FUZZ_PATH")
    if ossfuzz_env:
        path = Path(ossfuzz_env)
        if path.exists() and (path / "infra" / "helper.py").exists():
            return path

    # Check common locations
    common_locations = [
        Path.home() / "oss-fuzz",
        Path("/oss-fuzz"),
        Path("../oss-fuzz"),
        Path("../../oss-fuzz")
    ]

    for location in common_locations:
        if location.exists() and (location / "infra" / "helper.py").exists():
            return location

    return None


def parse_benchmark_config(benchmark_path: Path) -> Dict[str, Any]:
    """Parse benchmark configuration from .aixcc/config.yaml.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Configuration dictionary

    Raises:
        BuilderIntegrationError: If configuration is invalid
    """
    config_path = benchmark_path / ".aixcc" / "config.yaml"

    if not config_path.exists():
        raise BuilderIntegrationError(f"Configuration file not found: {config_path}")

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        logger.debug(f"Loaded benchmark config from {config_path}")
        return config

    except Exception as e:
        raise BuilderIntegrationError(f"Failed to parse configuration: {e}")


def parse_project_yaml(benchmark_path: Path) -> Dict[str, Any]:
    """Parse project.yaml for OSS-Fuzz projects.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Project configuration dictionary

    Raises:
        BuilderIntegrationError: If project.yaml is invalid
    """
    project_yaml = benchmark_path / "project.yaml"

    if not project_yaml.exists():
        raise BuilderIntegrationError(f"project.yaml not found: {project_yaml}")

    try:
        with open(project_yaml) as f:
            config = yaml.safe_load(f)

        logger.debug(f"Loaded project.yaml from {project_yaml}")
        return config

    except Exception as e:
        raise BuilderIntegrationError(f"Failed to parse project.yaml: {e}")


def create_ossfuzz_builder(
    benchmark_path: Path,
    ossfuzz_path: Optional[Path] = None,
    workspace: Optional[Path] = None
) -> OSSFuzzBuilder:
    """Create an OSS-Fuzz builder for a benchmark project.

    Args:
        benchmark_path: Path to benchmark directory
        ossfuzz_path: Path to OSS-Fuzz repository (auto-detected if None)
        workspace: Workspace directory for builds

    Returns:
        Configured OSSFuzzBuilder instance

    Raises:
        BuilderIntegrationError: If builder creation fails
    """
    # Auto-detect OSS-Fuzz path if not provided
    if ossfuzz_path is None:
        ossfuzz_path = get_ossfuzz_path()
        if ossfuzz_path is None:
            raise BuilderIntegrationError(
                "OSS-Fuzz repository not found. Set OSS_FUZZ_PATH environment variable "
                "or ensure oss-fuzz is in a standard location."
            )

    # Verify Docker is available
    if not check_docker_available():
        raise BuilderIntegrationError("Docker is required for OSS-Fuzz builds but is not available")

    # Parse project configuration
    try:
        project_config = parse_project_yaml(benchmark_path)
    except BuilderIntegrationError:
        # Try to get project name from benchmark path
        project_name = benchmark_path.name
        logger.warning(f"No project.yaml found, using benchmark name as project: {project_name}")
        project_config = {"language": "c"}
    else:
        project_name = benchmark_path.name  # Use directory name as project name

    # Get sanitizers from config
    sanitizers = []
    sanitizer_names = project_config.get("sanitizers", ["address"])

    for san_name in sanitizer_names:
        try:
            if san_name == "address":
                sanitizers.append(Sanitizer.AddressSanitizer)
            elif san_name == "memory":
                sanitizers.append(Sanitizer.MemorySanitizer)
            elif san_name == "undefined":
                sanitizers.append(Sanitizer.UndefinedBehaviorSanitizer)
            elif san_name == "thread":
                sanitizers.append(Sanitizer.ThreadSanitizer)
            elif san_name == "leak":
                sanitizers.append(Sanitizer.LeakAddressSanitizer)
            else:
                logger.warning(f"Unknown sanitizer '{san_name}', skipping")
        except Exception as e:
            logger.warning(f"Failed to parse sanitizer '{san_name}': {e}")

    if not sanitizers:
        sanitizers = [Sanitizer.AddressSanitizer]  # Default fallback

    # Determine source path
    # For OSS-Fuzz projects, source is typically cloned separately
    # For now, use the benchmark path as source path
    source_path = benchmark_path

    logger.info(f"Creating OSS-Fuzz builder for project '{project_name}' with sanitizers: {[s.value for s in sanitizers]}")

    return OSSFuzzBuilder(
        project=project_name,
        source_path=source_path,
        ossfuzz_path=ossfuzz_path,
        sanitizers=sanitizers,
        workspace=workspace
    )


def create_builder_from_config(
    benchmark_path: Path,
    builder_type: Optional[str] = None,
    **kwargs
) -> Builder:
    """Create a builder instance based on benchmark configuration.

    Args:
        benchmark_path: Path to benchmark directory
        builder_type: Override builder type detection
        **kwargs: Additional arguments for builder constructor

    Returns:
        Configured Builder instance

    Raises:
        BuilderIntegrationError: If builder creation fails
    """
    if builder_type is None:
        builder_type = detect_project_type(benchmark_path)

    logger.info(f"Creating builder for {builder_type} project at {benchmark_path}")

    if builder_type == "ossfuzz":
        return create_ossfuzz_builder(benchmark_path, **kwargs)
    else:
        raise BuilderIntegrationError(f"Unsupported builder type: {builder_type}")


def validate_builder_config(benchmark_path: Path) -> Dict[str, Any]:
    """Validate builder configuration for a benchmark.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Validation result dictionary with 'valid' bool and 'errors' list

    """
    result = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "project_type": "unknown"
    }

    try:
        # Detect project type
        project_type = detect_project_type(benchmark_path)
        result["project_type"] = project_type

        if project_type == "ossfuzz":
            # Validate OSS-Fuzz specific requirements

            # Check for project.yaml
            if not (benchmark_path / "project.yaml").exists():
                result["errors"].append("project.yaml not found")
                result["valid"] = False
            else:
                try:
                    parse_project_yaml(benchmark_path)
                except BuilderIntegrationError as e:
                    result["errors"].append(f"Invalid project.yaml: {e}")
                    result["valid"] = False

            # Check for OSS-Fuzz repository
            ossfuzz_path = get_ossfuzz_path()
            if ossfuzz_path is None:
                result["warnings"].append("OSS-Fuzz repository not found in standard locations")

            # Check Docker availability
            if not check_docker_available():
                result["errors"].append("Docker is required but not available")
                result["valid"] = False

        elif project_type == "standard":
            # Validate standard CRSBench format
            if not (benchmark_path / ".aixcc" / "config.yaml").exists():
                result["errors"].append(".aixcc/config.yaml not found")
                result["valid"] = False
            else:
                try:
                    parse_benchmark_config(benchmark_path)
                except BuilderIntegrationError as e:
                    result["errors"].append(f"Invalid config.yaml: {e}")
                    result["valid"] = False

        else:
            result["errors"].append("Unknown project type - no project.yaml or .aixcc/config.yaml found")
            result["valid"] = False

    except Exception as e:
        result["errors"].append(f"Validation error: {e}")
        result["valid"] = False

    return result


def get_supported_sanitizers(builder_type: str) -> List[str]:
    """Get list of supported sanitizers for a builder type.

    Args:
        builder_type: Type of builder

    Returns:
        List of supported sanitizer names
    """
    if builder_type == "ossfuzz":
        return [s.value for s in OSSFuzzBuilder({}).supported_sanitizers]
    else:
        return []


def convert_pov_to_poc(pov: POV, harness: HarnessFile, benchmark_path: Path) -> Optional[POC]:
    """Convert CRSBench POV to builder POC format.

    Args:
        pov: CRSBench POV object
        harness: Target harness file
        benchmark_path: Path to benchmark directory

    Returns:
        POC instance or None if conversion fails
    """
    try:
        # Convert POV dict to POC
        pov_dict = {
            "name": pov.name,
            "sanitizer": getattr(pov, "sanitizer", "address"),
            "error_token": getattr(pov, "error_token", None)
        }

        # Check if POV has file reference
        if hasattr(pov, "file") and pov.file:
            pov_path = benchmark_path / pov.file
            if pov_path.exists():
                return create_poc_from_file(
                    pov_path,
                    target_harness=harness.name,
                    expected_sanitizer=pov_dict["sanitizer"],
                    expected_error_token=pov_dict["error_token"]
                )

        # Try to find POV file in standard locations
        pov_dirs = [
            benchmark_path / ".aixcc" / "povs",
            benchmark_path / "povs",
            benchmark_path
        ]

        for pov_dir in pov_dirs:
            if pov_dir.exists():
                # Look for files matching POV name
                potential_files = list(pov_dir.glob(f"{pov.name}*"))
                if potential_files:
                    return create_poc_from_file(
                        potential_files[0],
                        target_harness=harness.name,
                        expected_sanitizer=pov_dict["sanitizer"],
                        expected_error_token=pov_dict["error_token"]
                    )

        logger.warning(f"Could not find POV file for {pov.name}")
        return None

    except Exception as e:
        logger.error(f"Failed to convert POV {pov.name} to POC: {e}")
        return None


def load_benchmark_povs(benchmark_path: Path) -> List[POC]:
    """Load all POVs from a benchmark directory.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        List of POC instances
    """
    pocs = []

    try:
        # Try to load from .aixcc/config.yaml
        if (benchmark_path / ".aixcc" / "config.yaml").exists():
            config = parse_benchmark_config(benchmark_path)

            for harness_config in config.get("harness_files", []):
                harness_name = harness_config.get("name", "unknown")

                for pov_config in harness_config.get("cpvs", []):
                    poc = convert_crsbench_pov_to_poc(pov_config, harness_name)
                    if poc:
                        pocs.append(poc)

        # Also look for POV files in standard directories
        pov_dirs = [
            benchmark_path / ".aixcc" / "povs",
            benchmark_path / "povs"
        ]

        for pov_dir in pov_dirs:
            if pov_dir.exists():
                for pov_file in pov_dir.iterdir():
                    if pov_file.is_file():
                        try:
                            poc = create_poc_from_file(
                                pov_file,
                                target_harness="unknown"
                            )
                            pocs.append(poc)
                        except Exception as e:
                            logger.warning(f"Failed to load POV from {pov_file}: {e}")

    except Exception as e:
        logger.error(f"Failed to load POVs from {benchmark_path}: {e}")

    logger.info(f"Loaded {len(pocs)} POCs from benchmark")
    return pocs