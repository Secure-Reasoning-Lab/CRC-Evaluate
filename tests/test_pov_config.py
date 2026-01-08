"""Unit tests for POVVerificationConfig.

Tests for crsbench/evaluation/verification/pov/config.py.
"""

from crsbench.evaluation.verification.pov.config import POVVerificationConfig


class TestPOVVerificationConfig:
    """Tests for POVVerificationConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = POVVerificationConfig()

        assert config.early_stop_enabled is False

    def test_early_stop_enabled_true(self) -> None:
        """Test enabling early stop."""
        config = POVVerificationConfig(early_stop_enabled=True)
        assert config.early_stop_enabled is True

    def test_config_serialization(self) -> None:
        """Test config can be serialized to dict."""
        config = POVVerificationConfig(
            early_stop_enabled=True,
        )

        data = config.model_dump()

        assert data["early_stop_enabled"] is True

    def test_config_from_dict(self) -> None:
        """Test config can be created from dict."""
        data = {
            "early_stop_enabled": True,
        }

        config = POVVerificationConfig(**data)

        assert config.early_stop_enabled is True
