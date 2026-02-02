"""Tests for crsbench.evaluation.cleanup module."""

import os
from pathlib import Path

import pytest

from crsbench.evaluation.cleanup import (
    cleanup_trial_directory,
    copy_trial_results,
)


class TestCopyTrialResults:
    """Tests for copy_trial_results()."""

    def test_copies_regular_files(self, tmp_path: Path) -> None:
        """Regular files are copied to destination."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text('{"status": "ok"}')
        (trial_dir / "log.txt").write_text("some log")

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "metadata.json").read_text() == '{"status": "ok"}'
        assert (dest / "log.txt").read_text() == "some log"

    def test_excludes_crs_build(self, tmp_path: Path) -> None:
        """crs-build/ directory is excluded from copy."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text("{}")
        crs_build = trial_dir / "crs-build"
        crs_build.mkdir()
        (crs_build / "big-artifact").write_text("x" * 1000)

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "metadata.json").exists()
        assert not (dest / "crs-build").exists()

    def test_excludes_oss_bugfind(self, tmp_path: Path) -> None:
        """.oss-bugfind/ directory is excluded from copy."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text("{}")
        oss_dir = trial_dir / ".oss-bugfind"
        oss_dir.mkdir()
        (oss_dir / "build-log").write_text("build output")

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "metadata.json").exists()
        assert not (dest / ".oss-bugfind").exists()

    def test_resolves_symlink_to_excluded_dir(self, tmp_path: Path) -> None:
        """Symlinks pointing into excluded dirs are resolved and copied as real files."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        # Create crs-build/out/ with actual content
        crs_build = trial_dir / "crs-build"
        crs_build.mkdir()
        out_real = crs_build / "out"
        out_real.mkdir()
        (out_real / "fuzzer_output.txt").write_text("crash data")
        (out_real / "corpus").mkdir()
        (out_real / "corpus" / "seed1").write_text("seed")

        # output/ is a symlink to crs-build/out/
        output_link = trial_dir / "output"
        output_link.symlink_to(out_real)

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        # crs-build/ excluded
        assert not (dest / "crs-build").exists()
        # output/ copied as real directory with content
        assert (dest / "output").exists()
        assert not (dest / "output").is_symlink()
        assert (dest / "output" / "fuzzer_output.txt").read_text() == "crash data"
        assert (dest / "output" / "corpus" / "seed1").read_text() == "seed"

    def test_resolves_file_symlink_to_excluded_dir(self, tmp_path: Path) -> None:
        """File symlinks pointing into excluded dirs are resolved and copied."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        crs_build = trial_dir / "crs-build"
        crs_build.mkdir()
        (crs_build / "result.log").write_text("log content")

        # Symlink a file into crs-build/
        link = trial_dir / "result.log"
        link.symlink_to(crs_build / "result.log")

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert not (dest / "crs-build").exists()
        assert (dest / "result.log").read_text() == "log content"
        assert not (dest / "result.log").is_symlink()

    def test_nonexistent_trial_dir(self, tmp_path: Path) -> None:
        """Non-existent trial dir logs warning and returns without error."""
        dest = tmp_path / "dest"
        copy_trial_results(tmp_path / "nonexistent", dest)
        assert not dest.exists()

    def test_symlink_not_pointing_to_excluded_is_preserved(self, tmp_path: Path) -> None:
        """Symlinks not pointing to excluded dirs are copied normally."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        # Create a regular dir and symlink to it (not excluded)
        data_dir = trial_dir / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("hello")

        link = trial_dir / "data-link"
        link.symlink_to(data_dir)

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "data" / "file.txt").read_text() == "hello"
        assert (dest / "data-link").exists()


class TestCleanupTrialDirectory:
    """Tests for cleanup_trial_directory()."""

    def test_deletes_excluded_dirs(self, tmp_path: Path) -> None:
        """Excluded directories are deleted."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text("{}")
        (trial_dir / "crs-build").mkdir()
        (trial_dir / "crs-build" / "artifact").write_text("big")
        (trial_dir / ".oss-bugfind").mkdir()

        cleanup_trial_directory(trial_dir)

        assert (trial_dir / "metadata.json").exists()
        assert not (trial_dir / "crs-build").exists()
        assert not (trial_dir / ".oss-bugfind").exists()

    def test_resolves_symlink_before_cleanup(self, tmp_path: Path) -> None:
        """Symlinks to excluded dirs are resolved before excluded dirs are deleted."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        crs_build = trial_dir / "crs-build"
        crs_build.mkdir()
        out_real = crs_build / "out"
        out_real.mkdir()
        (out_real / "result.txt").write_text("important")

        output_link = trial_dir / "output"
        output_link.symlink_to(out_real)

        cleanup_trial_directory(trial_dir)

        # crs-build deleted
        assert not (trial_dir / "crs-build").exists()
        # output/ resolved to real dir and preserved
        assert (trial_dir / "output" / "result.txt").read_text() == "important"
        assert not (trial_dir / "output").is_symlink()
