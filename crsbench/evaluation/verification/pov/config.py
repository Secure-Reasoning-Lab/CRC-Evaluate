"""Configuration for POV verification during CRS evaluation.

This module provides POVVerificationConfig for configuring POV verification
behavior. Follows the pattern of CoverageConfig with only used fields.
"""

from pydantic import BaseModel, Field


class POVVerificationConfig(BaseModel):
    """Configuration for POV verification during CRS evaluation.

    Controls whether early termination is enabled when all CPVs for a harness
    are discovered. Follows the pattern of CoverageConfig.

    Attributes:
        early_stop_enabled: Enable early termination when all CPVs found for harness
    """

    early_stop_enabled: bool = Field(
        default=False,
        description="Enable early termination when all CPVs found for harness",
    )
