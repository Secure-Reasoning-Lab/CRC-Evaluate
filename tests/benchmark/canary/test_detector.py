"""Tests for canary detector."""

import tempfile
import uuid
from pathlib import Path

from crsbench.benchmark.canary.detector import (
    check_uuid_in_response,
    create_detection_prompt,
    list_registered_canaries,
)
from crsbench.benchmark.canary.generator import save_registry
from crsbench.benchmark.canary.models import CANARY_WARNING, CanaryRegistry


class TestCreateDetectionPrompt:
    """Tests for create_detection_prompt function."""

    def test_prompt_contains_partial_uuid(self):
        """Test that prompt contains first 8 chars of UUID."""
        test_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        prompt = create_detection_prompt(test_uuid)

        assert "12345678" in prompt
        assert CANARY_WARNING in prompt

    def test_prompt_format(self):
        """Test that prompt has expected structure."""
        test_uuid = uuid.uuid4()
        prompt = create_detection_prompt(test_uuid)

        # Should have canary GUID prefix
        assert "canary GUID" in prompt
        # Should have partial UUID (first 8 chars)
        assert str(test_uuid)[:8] in prompt

    def test_prompt_asks_for_completion(self):
        """Test that prompt asks for UUID completion."""
        test_uuid = uuid.uuid4()
        prompt = create_detection_prompt(test_uuid)

        assert "complete" in prompt.lower() or "UUID" in prompt


class TestCheckUuidInResponse:
    """Tests for check_uuid_in_response function."""

    def test_exact_match(self):
        """Test detection of exact UUID match."""
        test_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        response = "Here is the UUID: 12345678-1234-5678-1234-567812345678"

        assert check_uuid_in_response(test_uuid, response) is True

    def test_no_match(self):
        """Test when UUID is not in response."""
        test_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        response = "I cannot complete this UUID"

        assert check_uuid_in_response(test_uuid, response) is False

    def test_case_insensitive(self):
        """Test that matching is case-insensitive."""
        test_uuid = uuid.UUID("abcdefab-abcd-abcd-abcd-abcdabcdabcd")
        response = "ABCDEFAB-ABCD-ABCD-ABCD-ABCDABCDABCD"

        assert check_uuid_in_response(test_uuid, response) is True

    def test_uuid_embedded_in_text(self):
        """Test detection when UUID is embedded in text."""
        test_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        response = (
            "Based on the pattern, I believe the complete UUID is "
            "12345678-1234-5678-1234-567812345678. Is that correct?"
        )

        assert check_uuid_in_response(test_uuid, response) is True


class TestListRegisteredCanaries:
    """Tests for list_registered_canaries function."""

    def test_empty_registry(self):
        """Test listing canaries from empty registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "registry.json"
            # File doesn't exist - should return empty dict
            canaries = list_registered_canaries(registry_path)
            assert canaries == {}

    def test_list_canaries(self):
        """Test listing canaries from populated registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "registry.json"

            # Create registry with some canaries
            registry = CanaryRegistry()
            uuid_atlanta = registry.get_or_create_uuid("atlanta-*")
            uuid_afc = registry.get_or_create_uuid("afc-*")
            save_registry(registry, registry_path)

            # List canaries
            canaries = list_registered_canaries(registry_path)

            assert len(canaries) == 2
            assert canaries["atlanta-*"] == uuid_atlanta
            assert canaries["afc-*"] == uuid_afc
