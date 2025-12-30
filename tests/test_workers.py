"""Tests for the workers utility module."""

import os
from unittest.mock import patch

from crsbench.utils.workers import (
    DEFAULT_WORKERS,
    WORKERS_ENV_VAR,
    resolve_workers,
)


class TestResolveWorkers:
    """Tests for resolve_workers function."""

    def test_cli_takes_highest_priority(self):
        """CLI argument should override all other sources."""
        with patch.dict(os.environ, {WORKERS_ENV_VAR: "8"}):
            result = resolve_workers(cli_workers=4, config_workers=2)
            assert result == 4

    def test_env_var_takes_priority_over_config(self):
        """Environment variable should override config."""
        with patch.dict(os.environ, {WORKERS_ENV_VAR: "8"}):
            result = resolve_workers(cli_workers=None, config_workers=2)
            assert result == 8

    def test_config_used_when_no_cli_or_env(self):
        """Config value should be used when CLI and env are not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Make sure env var is not set
            os.environ.pop(WORKERS_ENV_VAR, None)
            result = resolve_workers(cli_workers=None, config_workers=6)
            assert result == 6

    def test_default_used_when_nothing_specified(self):
        """Default value should be used when nothing is specified."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(WORKERS_ENV_VAR, None)
            result = resolve_workers(cli_workers=None, config_workers=None)
            assert result == DEFAULT_WORKERS

    def test_invalid_cli_workers_uses_next_priority(self):
        """Invalid CLI workers (< 1) should fall through to next priority."""
        with patch.dict(os.environ, {WORKERS_ENV_VAR: "5"}):
            result = resolve_workers(cli_workers=0, config_workers=3)
            assert result == 5

    def test_invalid_env_var_uses_config(self):
        """Invalid env var should fall through to config."""
        with patch.dict(os.environ, {WORKERS_ENV_VAR: "not_a_number"}):
            result = resolve_workers(cli_workers=None, config_workers=3)
            assert result == 3

    def test_negative_cli_workers_uses_next_priority(self):
        """Negative CLI workers should fall through to next priority."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(WORKERS_ENV_VAR, None)
            result = resolve_workers(cli_workers=-1, config_workers=3)
            assert result == 3

    def test_zero_config_workers_uses_default(self):
        """Zero config workers should use default."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(WORKERS_ENV_VAR, None)
            result = resolve_workers(cli_workers=None, config_workers=0)
            assert result == DEFAULT_WORKERS

    def test_default_workers_constant(self):
        """Verify DEFAULT_WORKERS constant is 4."""
        assert DEFAULT_WORKERS == 4

    def test_env_var_name_constant(self):
        """Verify WORKERS_ENV_VAR constant."""
        assert WORKERS_ENV_VAR == "CRSBENCH_WORKERS"
