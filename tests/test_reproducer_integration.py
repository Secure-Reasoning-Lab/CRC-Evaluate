"""Integration tests for OSSFuzzReproducer with mock helper.py."""

import shutil
from pathlib import Path

import pytest
from crsbench.validation.verification.reproducer import (
    OSSFuzzReproducer,
)


@pytest.fixture
def mock_oss_fuzz(tmp_path):
    """Create mock oss-fuzz directory with mock helper.py."""
    infra_dir = tmp_path / "infra"
    infra_dir.mkdir()

    # Copy mock helper.py
    mock_helper_src = Path(__file__).parent / "fixtures" / "mock_helper.py"
    shutil.copy(mock_helper_src, infra_dir / "helper.py")

    return tmp_path


@pytest.fixture
def reproducer(mock_oss_fuzz):
    """Create reproducer with mock oss-fuzz."""
    return OSSFuzzReproducer(mock_oss_fuzz, timeout=5)


class TestExitCodeHandling:
    """Test exit code handling with mock helper.py."""

    def test_no_crash_returns_false(self, reproducer):
        """Exit code 0 → False."""
        result = reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
        )
        assert result is False

    def test_asan_crash_returns_true(self, reproducer):
        """Exit code 77 (ASAN) → True."""
        result = reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"ASAN",
        )
        assert result is True

    def test_generic_crash_returns_true(self, reproducer):
        """Exit code 1 → True."""
        result = reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"CRASH",
        )
        assert result is True

    def test_timeout_returns_false(self, reproducer):
        """Exit code 124 (timeout) → False."""
        result = reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"TIMEOUT",
        )
        assert result is False

    def test_subprocess_timeout_returns_false(self, mock_oss_fuzz):
        """Subprocess timeout (HANG) → False."""
        # Use short timeout to trigger subprocess timeout
        reproducer = OSSFuzzReproducer(mock_oss_fuzz, timeout=1)
        result = reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"HANG",
        )
        assert result is False


class TestCommandConstruction:
    """Test that commands are constructed correctly."""

    def test_propagate_exit_codes_used(self, reproducer, caplog):
        """Command should include --propagate_exit_codes."""
        import logging

        caplog.set_level(logging.DEBUG)

        reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
        )

        assert "--propagate_exit_codes" in caplog.text

    def test_timeout_flag_used(self, reproducer, caplog):
        """Command should include --timeout."""
        import logging

        caplog.set_level(logging.DEBUG)

        reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
            timeout=120,
        )

        assert "--timeout" in caplog.text
        assert "120" in caplog.text

    def test_detect_leaks_disabled(self, reproducer, caplog):
        """Command should include -detect_leaks=0."""
        import logging

        caplog.set_level(logging.DEBUG)

        reproducer.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
        )

        assert "-detect_leaks=0" in caplog.text
