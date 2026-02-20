"""Tests for crsbench.evaluation.cleanup module."""

from pathlib import Path

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

    def test_excludes_crs_compose_workdir(self, tmp_path: Path) -> None:
        """oss-crs-workdir/ directory is excluded from copy."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text("{}")
        workdir = trial_dir / "oss-crs-workdir"
        workdir.mkdir()
        (workdir / "big-artifact").write_text("x" * 1000)

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "metadata.json").exists()
        assert not (dest / "oss-crs-workdir").exists()

    def test_excludes_staged(self, tmp_path: Path) -> None:
        """staged/ directory is excluded from copy."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        (trial_dir / "metadata.json").write_text("{}")
        staged = trial_dir / "staged"
        staged.mkdir()
        (staged / "benchmark-copy").write_text("benchmark data")

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert (dest / "metadata.json").exists()
        assert not (dest / "staged").exists()

    def test_resolves_symlink_to_excluded_dir(self, tmp_path: Path) -> None:
        """Symlinks pointing into excluded dirs are resolved and copied as real files."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        # Create oss-crs-workdir/out/ with actual content
        workdir = trial_dir / "oss-crs-workdir"
        workdir.mkdir()
        out_real = workdir / "out"
        out_real.mkdir()
        (out_real / "fuzzer_output.txt").write_text("crash data")
        (out_real / "seeds").mkdir()
        (out_real / "seeds" / "seed1").write_text("seed")

        # output/ is a symlink to oss-crs-workdir/out/
        output_link = trial_dir / "output"
        output_link.symlink_to(out_real)

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        # oss-crs-workdir/ excluded
        assert not (dest / "oss-crs-workdir").exists()
        # output/ copied as real directory with content
        assert (dest / "output").exists()
        assert not (dest / "output").is_symlink()
        assert (dest / "output" / "fuzzer_output.txt").read_text() == "crash data"
        assert (dest / "output" / "seeds" / "seed1").read_text() == "seed"

    def test_resolves_file_symlink_to_excluded_dir(self, tmp_path: Path) -> None:
        """File symlinks pointing into excluded dirs are resolved and copied."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        workdir = trial_dir / "oss-crs-workdir"
        workdir.mkdir()
        (workdir / "result.log").write_text("log content")

        # Symlink a file into oss-crs-workdir/
        link = trial_dir / "result.log"
        link.symlink_to(workdir / "result.log")

        dest = tmp_path / "dest"
        copy_trial_results(trial_dir, dest)

        assert not (dest / "oss-crs-workdir").exists()
        assert (dest / "result.log").read_text() == "log content"
        assert not (dest / "result.log").is_symlink()

    def test_nonexistent_trial_dir(self, tmp_path: Path) -> None:
        """Non-existent trial dir logs warning and returns without error."""
        dest = tmp_path / "dest"
        copy_trial_results(tmp_path / "nonexistent", dest)
        assert not dest.exists()

    def test_symlink_not_pointing_to_excluded_is_preserved(
        self, tmp_path: Path
    ) -> None:
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
        (trial_dir / "oss-crs-workdir").mkdir()
        (trial_dir / "oss-crs-workdir" / "artifact").write_text("big")
        (trial_dir / "staged").mkdir()

        cleanup_trial_directory(trial_dir)

        assert (trial_dir / "metadata.json").exists()
        assert not (trial_dir / "oss-crs-workdir").exists()
        assert not (trial_dir / "staged").exists()

    def test_resolves_symlink_before_cleanup(self, tmp_path: Path) -> None:
        """Symlinks to excluded dirs are resolved before excluded dirs are deleted."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        workdir = trial_dir / "oss-crs-workdir"
        workdir.mkdir()
        out_real = workdir / "out"
        out_real.mkdir()
        (out_real / "result.txt").write_text("important")

        output_link = trial_dir / "output"
        output_link.symlink_to(out_real)

        cleanup_trial_directory(trial_dir)

        # oss-crs-workdir deleted
        assert not (trial_dir / "oss-crs-workdir").exists()
        # output/ resolved to real dir and preserved
        assert (trial_dir / "output" / "result.txt").read_text() == "important"
        assert not (trial_dir / "output").is_symlink()
