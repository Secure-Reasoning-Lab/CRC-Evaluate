"""
Utility functions for vuln.yaml generation.

This module contains the main entry point for generating vuln.yaml files
for CPVs in benchmarks.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def generate_vuln_yaml_for_cpv(
    benchmark_name: str,
    benchmark_dir: str,
    harness_name: str,
    cpv_id: str,
    project_dir: str,
    litellm_base_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
    *,
    force: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Generate vuln.yaml for a specific CPV.

    Args:
        benchmark_name: Name of the benchmark
        benchmark_dir: Path to benchmark directory (contains .aixcc/)
        harness_name: Name of the harness
        cpv_id: CPV identifier (e.g., "cpv_0")
        project_dir: Path to project source repository
        litellm_base_url: Optional LiteLLM proxy URL
        litellm_api_key: Optional LiteLLM API key
        force: Overwrite existing vuln.yaml
        verbose: Enable verbose logging

    Returns:
        Dictionary with generation results:
        {
            "success": bool,
            "vuln_yaml_path": str,
            "analysis_md_path": str,
            "agent_log_path": str,
            "message": str
        }
    """
    # Import here to avoid circular imports
    from crsbench.migration.test_sh import copy_benchmark_files_to_project
    from crsbench.migration.vuln_yaml.generator import VulnYamlGenerator

    benchmark_path = Path(benchmark_dir)
    project_path = Path(project_dir)

    # Validate directories
    if not benchmark_path.is_dir():
        return {
            "success": False,
            "message": f"Benchmark directory not found: {benchmark_dir}",
        }

    if not project_path.is_dir():
        return {
            "success": False,
            "message": f"Project directory not found: {project_dir}",
        }

    # Locate CPV directory
    cpv_path = benchmark_path / ".aixcc" / harness_name / cpv_id
    if not cpv_path.is_dir():
        return {"success": False, "message": f"CPV directory not found: {cpv_path}"}

    # Check if vuln.yaml already exists
    vuln_yaml_path = cpv_path / "vuln.yaml"
    is_temporary = False

    if vuln_yaml_path.exists():
        # Check if it's a temporary/MOCK file
        try:
            content = vuln_yaml_path.read_text()
            # Check for MOCK or TBD markers indicating temporary content
            if "MOCK:" in content or "(TBD)" in content:
                is_temporary = True
                if verbose:
                    logger.debug(
                        "Found temporary vuln.yaml (contains MOCK/TBD), will regenerate..."
                    )
        except Exception as e:
            if verbose:
                logger.warning(f"Could not read existing vuln.yaml: {e}")

        # If not temporary and not force, skip (not an error)
        if not is_temporary and not force:
            return {
                "success": True,
                "skipped": True,
                "message": "vuln.yaml already exists, skipped",
            }

    # Create generator
    generator = VulnYamlGenerator(
        litellm_base_url=litellm_base_url, litellm_api_key=litellm_api_key
    )

    # Copy OSS-Fuzz files to project directory
    if verbose:
        logger.debug(f"Copying OSS-Fuzz files to {project_dir}/.oss-fuzz/...")

    copy_benchmark_files_to_project(benchmark_dir, project_dir, verbose=verbose)

    # Step 1: Analyze vulnerability
    if verbose:
        logger.debug(f"Analyzing vulnerability {cpv_id} in {harness_name}...")

    analysis_md, analysis_log = generator.analyze_vulnerability_sync(
        project_dir, str(cpv_path), cpv_id, harness_name, verbose=verbose
    )

    # Save analysis markdown
    analysis_md_path = cpv_path / "vuln_analysis.md"
    with analysis_md_path.open("w") as f:
        f.write(analysis_md)

    if verbose:
        logger.debug(f"Vulnerability analysis saved to {analysis_md_path}")

    # Step 2: Generate vuln.yaml with validation and retry
    if verbose:
        logger.debug("Generating vuln.yaml with validation...")

    vuln_yaml_content, generation_log, validation_errors = (
        generator.generate_and_validate_vuln_yaml_sync(
            analysis_md, cpv_id, harness_name, max_retries=2, verbose=verbose
        )
    )

    # Save vuln.yaml
    with vuln_yaml_path.open("w") as f:
        f.write(vuln_yaml_content)

    if verbose:
        if validation_errors:
            logger.warning(
                f"vuln.yaml generated with {len(validation_errors)} remaining validation error(s) at {vuln_yaml_path}"
            )
        else:
            logger.debug(f"vuln.yaml generated and validated at {vuln_yaml_path}")

    # Step 3: Save combined agent log
    agent_log_path = cpv_path / "vuln_agent_log.txt"

    # Include validation status in log
    validation_status = ""
    if validation_errors:
        validation_status = "\n\nValidation Errors (after retries):\n"
        for err in validation_errors:
            validation_status += f"  - {err}\n"
    else:
        validation_status = "\n\nValidation: PASSED\n"

    combined_log = f"""# vuln.yaml Generation Agent Log
Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Harness: {harness_name}
CPV: {cpv_id}
Project Directory: {project_dir}
{validation_status}
{analysis_log}
{generation_log}
"""
    with agent_log_path.open("w") as f:
        f.write(combined_log)

    if verbose:
        logger.debug(f"Agent log saved to {agent_log_path}")

    # Return success even with validation errors (best effort)
    # The validation errors are logged for manual review
    message = f"Successfully generated vuln.yaml for {cpv_id}"
    if validation_errors:
        message += f" (with {len(validation_errors)} validation warning(s))"

    return {
        "success": True,
        "vuln_yaml_path": str(vuln_yaml_path),
        "analysis_md_path": str(analysis_md_path),
        "agent_log_path": str(agent_log_path),
        "message": message,
        "validation_errors": [str(e) for e in validation_errors],
    }
