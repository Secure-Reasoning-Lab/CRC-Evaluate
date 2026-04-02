"""Tests for persisted storage warnings under /tmp."""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def storage_warning_module():
    """Reload the module so each test starts with a clean dedupe state."""
    import crsbench.utils.storage_warning as storage_warning

    return importlib.reload(storage_warning)


def reload_storage_warning_with_resolve(monkeypatch, resolve_impl):
    """Reload storage_warning after pinning Path.resolve behavior."""
    monkeypatch.setattr(Path, "resolve", resolve_impl)
    import crsbench.utils.storage_warning as storage_warning

    return importlib.reload(storage_warning)


def raise_if_resolve_called(*args, **kwargs):  # noqa: ARG001
    """Fail tests that accidentally reintroduce Path.resolve dependence."""
    raise AssertionError("resolve called")


def test_warns_for_tmp_roots_and_nested_paths(storage_warning_module):
    """Paths at /tmp and below should warn with advisory guidance."""
    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(Path("/tmp")) is True
        )
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/tmp/experiment")
            )
            is True
        )

    assert mock_warning.call_count == 2
    message = mock_warning.call_args_list[0].args[0]
    assert "tmpfs" in message
    assert "RAM" in message
    assert "large-scale experiments" in message
    assert "another location" in message


def test_warn_for_persisted_storage_roots_skips_inactive_results(
    storage_warning_module,
):
    """results_filestore should only be checked when copy-results is active."""
    with patch.object(
        storage_warning_module, "warn_if_persisted_storage_path"
    ) as mock_warn:
        storage_warning_module.warn_for_persisted_storage_roots(
            experiment_filestore=Path("/tmp/experiment"),
            report_filestore=Path("/tmp/report"),
            copy_results_after_trial=False,
            results_filestore=Path("/tmp/results"),
        )

    assert mock_warn.call_args_list == [
        ((Path("/tmp/experiment"),), {}),
        ((Path("/tmp/report"),), {}),
    ]


def test_warn_for_persisted_storage_roots_includes_active_results_and_remote(
    storage_warning_module,
):
    """Active results and remote roots should both be checked."""
    with patch.object(
        storage_warning_module, "warn_if_persisted_storage_path"
    ) as mock_warn:
        storage_warning_module.warn_for_persisted_storage_roots(
            experiment_filestore=Path("/tmp/experiment"),
            report_filestore=Path("/tmp/report"),
            copy_results_after_trial=True,
            results_filestore=Path("/tmp/results"),
            remote_experiment_root=Path("/tmp/remote"),
        )

    assert mock_warn.call_args_list == [
        ((Path("/tmp/experiment"),), {}),
        ((Path("/tmp/report"),), {}),
        ((Path("/tmp/results"),), {}),
        ((Path("/tmp/remote"),), {}),
    ]


def test_does_not_warn_for_similar_non_tmp_prefix(storage_warning_module):
    """Paths like /tmp2 should not match the /tmp warning rule."""
    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(Path("/tmp2"))
            is False
        )

    mock_warning.assert_not_called()


def test_warns_once_for_normalized_equivalent_paths(storage_warning_module):
    """Normalized equivalent paths should deduplicate repeated warnings."""
    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/tmp/../tmp/experiment")
            )
            is True
        )
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/tmp/experiment")
            )
            is False
        )

    mock_warning.assert_called_once()


def test_falls_back_when_path_resolution_fails(storage_warning_module, monkeypatch):
    """Resolution failures should not raise and should still allow advisory warnings."""

    def raise_oserror(*args, **kwargs):  # noqa: ARG001
        raise OSError("resolution failed")

    monkeypatch.setattr(Path, "resolve", raise_oserror)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(Path("/tmp/fallback"))
            is True
        )

    mock_warning.assert_called_once()


def test_warns_for_relative_tmp_path_when_resolution_fails(
    storage_warning_module, monkeypatch
):
    """Relative paths under a /tmp cwd should still warn if resolution fails."""

    def raise_oserror(*args, **kwargs):  # noqa: ARG001
        raise OSError("resolution failed")

    monkeypatch.setattr(Path, "cwd", lambda: Path("/tmp/workspace"))
    monkeypatch.setattr(Path, "resolve", raise_oserror)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(Path("experiment"))
            is True
        )

    mock_warning.assert_called_once()


def test_relative_tmp_fallback_does_not_crash_when_cwd_unavailable(
    storage_warning_module, monkeypatch
):
    """Fallback normalization should not crash if cwd cannot be resolved."""

    def raise_oserror(*args, **kwargs):  # noqa: ARG001
        raise OSError("resolution failed")

    monkeypatch.setattr(Path, "resolve", raise_oserror)
    monkeypatch.setattr(
        Path, "cwd", lambda: (_ for _ in ()).throw(FileNotFoundError("cwd missing"))
    )
    monkeypatch.setattr(
        os,
        "getcwd",
        lambda: (_ for _ in ()).throw(FileNotFoundError("cwd missing")),
    )

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(Path("experiment"))
            is False
        )

    mock_warning.assert_not_called()


def test_private_tmp_does_not_warn_on_linux_even_if_resolve_targets_tmp(
    monkeypatch,
):
    """Linux-only semantics should ignore non-/tmp aliases even if resolve maps them."""

    def fake_resolve(*args, **kwargs):  # noqa: ARG001
        self = args[0]
        if self == Path("/private/tmp/data"):
            return Path("/tmp/data")
        return Path(self)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    import crsbench.utils.storage_warning as storage_warning

    storage_warning_module = importlib.reload(storage_warning)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/private/tmp/data")
            )
            is False
        )

    mock_warning.assert_not_called()


def test_relative_path_escapes_out_of_tmp_and_does_not_warn(
    storage_warning_module, monkeypatch
):
    """Lexical normalization should not warn when a relative path escapes /tmp."""

    monkeypatch.setattr(Path, "cwd", lambda: Path("/tmp/workspace/subdir"))
    monkeypatch.setattr(Path, "resolve", raise_if_resolve_called)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("../../../var/data")
            )
            is False
        )

    mock_warning.assert_not_called()


def test_relative_path_stays_under_tmp_lexically_even_if_resolve_would_escape(
    storage_warning_module, monkeypatch
):
    """Lexical normalization should win over a hypothetical resolve-based escape."""

    monkeypatch.setattr(Path, "cwd", lambda: Path("/tmp/link/subdir"))
    monkeypatch.setattr(Path, "resolve", raise_if_resolve_called)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("../../var/data")
            )
            is True
        )

    mock_warning.assert_called_once()


def test_does_not_warn_when_path_escapes_tmp_prefix(storage_warning_module):
    """Paths that normalize outside /tmp should not warn."""
    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/tmp/../var/data")
            )
            is False
        )

    mock_warning.assert_not_called()


def test_does_not_warn_when_escape_path_falls_back_after_resolution_failure(
    storage_warning_module, monkeypatch
):
    """Fallback normalization should keep /tmp/../var/data outside tmp."""

    def raise_oserror(*args, **kwargs):  # noqa: ARG001
        raise OSError("resolution failed")

    monkeypatch.setattr(Path, "resolve", raise_oserror)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/tmp/../var/data")
            )
            is False
        )

    mock_warning.assert_not_called()


def test_warns_for_noncanonical_absolute_tmp_path_when_resolution_fails(
    storage_warning_module, monkeypatch
):
    """Non-canonical absolute paths targeting /tmp should still warn if resolution fails."""

    def raise_oserror(*args, **kwargs):  # noqa: ARG001
        raise OSError("resolution failed")

    monkeypatch.setattr(Path, "resolve", raise_oserror)

    with patch.object(storage_warning_module.logger, "warning") as mock_warning:
        assert (
            storage_warning_module.warn_if_persisted_storage_path(
                Path("/var/../tmp/data")
            )
            is True
        )

    mock_warning.assert_called_once()
