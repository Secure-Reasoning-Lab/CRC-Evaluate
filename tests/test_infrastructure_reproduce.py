"""Integration tests for OSSFuzzInfrastructure.reproduce() with mock helper.py."""

import shutil
from pathlib import Path

import pytest
from crsbench.builder.infrastructure import OSSFuzzInfrastructure


@pytest.fixture
def mock_oss_fuzz(tmp_path):
    """Create mock oss-fuzz directory with mock helper.py."""
    infra_dir = tmp_path / "infra"
    infra_dir.mkdir()

    # Copy mock helper.py
    mock_helper_src = Path(__file__).parent / "fixtures" / "mock_helper.py"
    shutil.copy(mock_helper_src, infra_dir / "helper.py")

    # Create projects directory (required by OSSFuzzInfrastructure)
    (tmp_path / "projects").mkdir()

    return tmp_path


@pytest.fixture
def infra(mock_oss_fuzz):
    """Create OSSFuzzInfrastructure with mock oss-fuzz."""
    return OSSFuzzInfrastructure(mock_oss_fuzz)


class TestExitCodeHandling:
    """Test exit code handling with mock helper.py."""

    def test_no_crash_returns_false(self, infra):
        """Exit code 0 → False."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
            timeout=5,
        )
        assert result is False

    def test_asan_crash_returns_true(self, infra):
        """Exit code 77 (ASAN) → True."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"ASAN",
            timeout=5,
        )
        assert result is True

    def test_generic_crash_returns_true(self, infra):
        """Exit code 1 → True."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"CRASH",
            timeout=5,
        )
        assert result is True

    def test_timeout_returns_false(self, infra):
        """Exit code 124 (timeout) → False."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"TIMEOUT",
            timeout=5,
        )
        assert result is False

    def test_subprocess_timeout_returns_false(self, infra):
        """Subprocess timeout (HANG) → False."""
        # Use short timeout to trigger subprocess timeout
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"HANG",
            timeout=1,
        )
        assert result is False


class TestCommandConstruction:
    """Test that commands are constructed correctly by mocking subprocess.run."""

    def test_propagate_exit_codes_used(self, mock_oss_fuzz):
        """Command should include --propagate_exit_codes."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=5,
            )

            # Verify the command was called with --propagate_exit_codes
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            assert "--propagate_exit_codes" in cmd

    def test_timeout_flag_used(self, mock_oss_fuzz):
        """Command should include --timeout."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=120,
            )

            # Verify the command was called with --timeout
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            assert "--timeout" in cmd
            # Find the --timeout index and check the next arg is 120
            timeout_idx = cmd.index("--timeout")
            assert cmd[timeout_idx + 1] == "120"

    def test_detect_leaks_disabled(self, mock_oss_fuzz):
        """Command should include -detect_leaks=0."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=5,
            )

            # Verify the command was called with -detect_leaks=0
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            assert "-detect_leaks=0" in cmd
