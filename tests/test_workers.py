"""Tests for the workers utility module."""

from crsbench.utils.workers import (
    DEFAULT_WORKERS,
    resolve_build_workers,
    resolve_verify_workers,
)


class TestResolveBuildWorkers:
    """Tests for resolve_build_workers function."""

    def test_cli_takes_highest_priority(self):
        """CLI argument should override config."""
        result = resolve_build_workers(cli_workers=4, config_workers=2)
        assert result == 4

    def test_config_used_when_no_cli(self):
        """Config value should be used when CLI is not set."""
        result = resolve_build_workers(cli_workers=None, config_workers=6)
        assert result == 6

    def test_default_used_when_nothing_specified(self):
        """Default value should be used when nothing is specified."""
        result = resolve_build_workers(cli_workers=None, config_workers=None)
        assert result == DEFAULT_WORKERS

    def test_invalid_cli_workers_uses_config(self):
        """Invalid CLI workers (< 1) should fall through to config."""
        result = resolve_build_workers(cli_workers=0, config_workers=3)
        assert result == 3

    def test_negative_cli_workers_uses_config(self):
        """Negative CLI workers should fall through to config."""
        result = resolve_build_workers(cli_workers=-1, config_workers=3)
        assert result == 3

    def test_zero_config_workers_uses_default(self):
        """Zero config workers should use default."""
        result = resolve_build_workers(cli_workers=None, config_workers=0)
        assert result == DEFAULT_WORKERS


class TestResolveVerifyWorkers:
    """Tests for resolve_verify_workers function."""

    def test_cli_takes_highest_priority(self):
        """CLI argument should override config."""
        result = resolve_verify_workers(cli_workers=4, config_workers=2)
        assert result == 4

    def test_config_used_when_no_cli(self):
        """Config value should be used when CLI is not set."""
        result = resolve_verify_workers(cli_workers=None, config_workers=6)
        assert result == 6

    def test_default_used_when_nothing_specified(self, monkeypatch):
        """Default value should be used when nothing is specified."""
        monkeypatch.delenv("OSS_FUZZ_CPUSET_CPUS", raising=False)
        result = resolve_verify_workers(cli_workers=None, config_workers=None)
        assert result == DEFAULT_WORKERS

    def test_cpuset_env_used_when_no_cli_or_config(self, monkeypatch):
        """Supervisor cpuset width should drive verify parallelism by default."""
        monkeypatch.setenv("OSS_FUZZ_CPUSET_CPUS", "0-15")
        result = resolve_verify_workers(cli_workers=None, config_workers=None)
        assert result == 16

    def test_invalid_cpuset_env_falls_back_to_default(self, monkeypatch):
        """Malformed cpuset env should not break worker resolution."""
        monkeypatch.setenv("OSS_FUZZ_CPUSET_CPUS", "invalid")
        result = resolve_verify_workers(cli_workers=None, config_workers=None)
        assert result == DEFAULT_WORKERS


class TestConstants:
    """Tests for module constants."""

    def test_default_workers_constant(self):
        """Verify DEFAULT_WORKERS constant is 4."""
        assert DEFAULT_WORKERS == 4
