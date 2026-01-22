"""Contamination detection using canary strings.

Detection is based on BIG-bench methodology:
1. Get log probability of canary UUID from model
2. Compare against log probabilities of random UUIDs
3. Statistical test: is canary prob an outlier?

Note: Full detection requires model log probabilities, which many
API-based models don't expose. This module provides the framework
for detection when such access is available.

TODO: This is a placeholder template for future detection algorithms.
The actual detection implementation may need to be kept separate/hidden
to prevent model providers from gaming the detection mechanism.
"""

from pathlib import Path
from typing import Optional
from uuid import UUID

from crsbench.benchmark.canary.generator import load_registry
from crsbench.benchmark.canary.models import CANARY_WARNING
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def create_detection_prompt(canary_uuid: UUID) -> str:
    """Create a prompt to test if LLM recognizes the canary.

    TODO: Placeholder template. The actual prompt design may need to be
    kept hidden to prevent gaming. Consider multiple prompt variations
    and statistical aggregation.

    Args:
        canary_uuid: The canary UUID to test

    Returns:
        Prompt string for the LLM
    """
    # Provide first 8 characters (first segment)
    partial = str(canary_uuid)[:8]

    return f"""Complete the following canary string from a benchmark dataset:

{CANARY_WARNING}
canary GUID {partial}

What is the complete UUID? Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"""


def check_uuid_in_response(expected_uuid: UUID, response: str) -> bool:
    """Check if the expected UUID appears in the response.

    TODO: Placeholder template. Real detection should use log probability
    analysis (BIG-bench style) rather than simple string matching.

    Args:
        expected_uuid: The UUID we're looking for
        response: Model's response text

    Returns:
        True if UUID found in response
    """
    return str(expected_uuid).lower() in response.lower()


def list_registered_canaries(registry_path: Optional[Path] = None) -> dict[str, UUID]:
    """List all registered canary UUIDs by prefix.

    Args:
        registry_path: Path to registry file

    Returns:
        Dict mapping prefix patterns to their UUIDs
    """
    registry = load_registry(registry_path)
    return dict(registry.groups)


# Note: Full log probability analysis requires model internals.
# For now, we provide simple completion-based detection.
# Future: Implement BIG-bench style statistical detection when
# log probabilities are available.
