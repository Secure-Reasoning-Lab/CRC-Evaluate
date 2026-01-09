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
        """Exit code 0 → crashed=False."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"OK",
            timeout=5,
        )
        assert result.crashed is False

    def test_asan_crash_returns_true(self, infra):
        """Exit code 77 (ASAN) → crashed=True."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"ASAN",
            timeout=5,
        )
        assert result.crashed is True

    def test_generic_crash_returns_true(self, infra):
        """Exit code 1 → crashed=True."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"CRASH",
            timeout=5,
        )
        assert result.crashed is True

    def test_timeout_returns_false(self, infra):
        """Exit code 124 (timeout) → crashed=False."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"TIMEOUT",
            timeout=5,
        )
        assert result.crashed is False


class TestTimeoutHandling:
    """Test subprocess timeout handling (mocked to avoid 30s grace period wait)."""

    def test_subprocess_timeout_returns_false(self, mock_oss_fuzz):
        """Subprocess TimeoutExpired → crashed=False."""
        import subprocess
        from unittest.mock import patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=1)
            result = infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"HANG",
                timeout=1,
            )
        assert result.crashed is False


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


class TestEnsureOssFuzzReady:
    """Test race condition prevention in ensure_oss_fuzz_ready()."""

    def test_creates_build_directory(self, tmp_path):
        """Build directory is created on first call."""
        from crsbench.builder.infrastructure import (
            _initialized_oss_fuzz_paths,
            ensure_oss_fuzz_ready,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        # Clear the cache for test isolation
        _initialized_oss_fuzz_paths.discard(oss_fuzz.resolve())

        ensure_oss_fuzz_ready(oss_fuzz)

        assert (oss_fuzz / "build").exists()
        assert (oss_fuzz / "build").is_dir()

    def test_idempotent_multiple_calls(self, tmp_path):
        """Multiple calls don't cause errors."""
        from crsbench.builder.infrastructure import (
            _initialized_oss_fuzz_paths,
            ensure_oss_fuzz_ready,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        _initialized_oss_fuzz_paths.discard(oss_fuzz.resolve())

        # Call multiple times - should not raise
        ensure_oss_fuzz_ready(oss_fuzz)
        ensure_oss_fuzz_ready(oss_fuzz)
        ensure_oss_fuzz_ready(oss_fuzz)

        assert (oss_fuzz / "build").exists()

    def test_caches_initialized_paths(self, tmp_path):
        """Initialized paths are cached to avoid redundant operations."""
        from crsbench.builder.infrastructure import (
            _initialized_oss_fuzz_paths,
            ensure_oss_fuzz_ready,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        _initialized_oss_fuzz_paths.discard(oss_fuzz.resolve())

        ensure_oss_fuzz_ready(oss_fuzz)

        assert oss_fuzz.resolve() in _initialized_oss_fuzz_paths

    def test_skips_if_already_initialized(self, tmp_path):
        """Skips mkdir if path already in cache."""
        from unittest.mock import patch

        from crsbench.builder.infrastructure import (
            _initialized_oss_fuzz_paths,
            ensure_oss_fuzz_ready,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        # Pre-add to cache
        _initialized_oss_fuzz_paths.add(oss_fuzz.resolve())

        # mkdir should not be called since path is cached
        with patch.object(Path, "mkdir") as mock_mkdir:
            ensure_oss_fuzz_ready(oss_fuzz)
            mock_mkdir.assert_not_called()
