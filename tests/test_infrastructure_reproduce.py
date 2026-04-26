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

        with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=1)
            result = infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"HANG",
                timeout=1,
            )
        assert result.crashed is False


class TestContainerCleanup:
    """Test reproduce container naming and explicit cleanup."""

    def test_reproduce_uses_unique_named_containers(self, mock_oss_fuzz):
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=5,
            )
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK-2",
                timeout=5,
            )

        container_names = [
            call.kwargs["env"]["OSS_FUZZ_SAVE_CONTAINERS_NAME"]
            for call in mock_run.call_args_list
        ]
        assert len(container_names) == 2
        assert all(
            container_name.startswith("crsbench-repro-")
            for container_name in container_names
        )
        assert len(set(container_names)) == 2

    def test_reproduce_does_not_cleanup_inline_on_normal_completion(
        self, mock_oss_fuzz
    ):
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with (
            patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
            patch("crsbench.builder.infrastructure.subprocess.run") as mock_cleanup,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=5,
            )

        mock_cleanup.assert_not_called()

    def test_timeout_cleans_up_named_container_before_returning(self, mock_oss_fuzz):
        import subprocess
        import threading
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        result_holder: dict[str, object] = {}

        def _cleanup_side_effect(*_args, **_kwargs):
            cleanup_started.set()
            assert release_cleanup.wait(timeout=1)
            return MagicMock(returncode=0, stdout="", stderr="")

        def _run_reproduce():
            result_holder["result"] = infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"HANG",
                timeout=1,
            )

        with (
            patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
            patch("crsbench.builder.infrastructure.subprocess.run") as mock_cleanup,
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=1)
            mock_cleanup.side_effect = _cleanup_side_effect

            reproduce_thread = threading.Thread(target=_run_reproduce)
            reproduce_thread.start()
            assert cleanup_started.wait(timeout=1)
            reproduce_thread.join(timeout=0.1)
            assert reproduce_thread.is_alive()
            release_cleanup.set()
            reproduce_thread.join(timeout=1)

        assert not reproduce_thread.is_alive()
        result = result_holder["result"]
        assert result.crashed is False
        env = mock_run.call_args.kwargs["env"]
        container_name = env["OSS_FUZZ_SAVE_CONTAINERS_NAME"]
        mock_cleanup.assert_called_once_with(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            stdin=subprocess.DEVNULL,
        )


class TestCommandConstruction:
    """Test that commands are constructed correctly by mocking subprocess.run."""

    def test_propagate_exit_codes_not_used(self, mock_oss_fuzz):
        """Official helper.py reproduce does not support --propagate_exit_codes."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=5,
            )

            # Verify legacy flag is not used
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            assert "--propagate_exit_codes" not in cmd

    def test_timeout_flag_not_used(self, mock_oss_fuzz):
        """Official helper.py reproduce does not support --timeout."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        requested_timeout = 120

        with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            infra.reproduce(
                project_name="test",
                harness="fuzz",
                pov_data=b"OK",
                timeout=requested_timeout,
            )

            # Verify legacy timeout flag is not used
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            assert "--timeout" not in cmd
            # Preserve the historical timeout + 10s outer bound by reserving
            # 5s for helper exit semantics and 5s for forced cleanup.
            assert mock_run.call_args.kwargs["timeout"] > requested_timeout
            assert mock_run.call_args.kwargs["timeout"] == requested_timeout + 5

    def test_detect_leaks_disabled(self, mock_oss_fuzz):
        """Command should include -detect_leaks=0."""
        from unittest.mock import MagicMock, patch

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
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


class TestLeakSanitizerHandling:
    """Test LeakSanitizer-only exits are not treated as crashes."""

    def test_leak_only_exit_not_crash(self, infra):
        """LeakSanitizer-only exit → crashed=False."""
        result = infra.reproduce(
            project_name="test",
            harness="fuzz",
            pov_data=b"LEAK",
            timeout=5,
        )
        assert result.crashed is False
        assert result.exit_code == 1

    def test_is_leak_only_exit_helper(self):
        """Unit test for _is_leak_only_exit()."""
        from crsbench.builder.infrastructure import (
            _has_crash_signature,
            _is_leak_only_exit,
        )

        # Leak only → True
        assert _is_leak_only_exit(
            "==ERROR: LeakSanitizer: detected memory leaks\nSUMMARY: 5600 bytes leaked"
        )

        # No leak → False
        assert not _is_leak_only_exit(
            "AddressSanitizer:DEADLYSIGNAL\n==ERROR: AddressSanitizer: SEGV"
        )

        # No markers at all → False
        assert not _is_leak_only_exit("INFO: Running with entropic power schedule")

        # Crash + leak markers → not leak-only
        mixed_output = (
            "runtime error: signed integer overflow\n"
            "==ERROR: LeakSanitizer: detected memory leaks\n"
            "SUMMARY: AddressSanitizer: 64 byte(s) leaked"
        )
        assert _has_crash_signature(mixed_output)
        assert not _is_leak_only_exit(mixed_output)

        # ASAN CHECK failed output should be treated as crash signature
        assert _has_crash_signature(
            "AddressSanitizer: CHECK failed: sanitizer_posix.cpp"
        )


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
