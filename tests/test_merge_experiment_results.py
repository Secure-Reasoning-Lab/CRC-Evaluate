"""Tests for merge_experiment_results.py script."""

from pathlib import Path

import pytest
from scripts.merge_experiment_results import (
    TrialInfo,
    detect_conflicts,
    enumerate_trials,
    get_trial_status,
    merge_trials,
    parse_trial_path,
)


class TestParsing:
    """Test path parsing functions."""

    def test_parse_trial_path_valid(self):
        """Test parsing valid trial path."""
        path = Path("crs1/bench1/harness1/bugfinding/address/trial-0")
        crs, benchmark, harness, mode, sanitizer, trial_num = parse_trial_path(path)

        assert crs == "crs1"
        assert benchmark == "bench1"
        assert harness == "harness1"
        assert mode == "bugfinding"
        assert sanitizer == "address"
        assert trial_num == 0

    def test_parse_trial_path_invalid_format(self):
        """Test parsing invalid trial path."""
        path = Path("crs1/bench1/harness1/trial-0")  # Missing components

        with pytest.raises(ValueError, match="Invalid trial path format"):
            parse_trial_path(path)

    def test_parse_trial_path_invalid_trial_dirname(self):
        """Test parsing path with invalid trial directory name."""
        path = Path("crs1/bench1/harness1/bugfinding/address/foo-0")

        with pytest.raises(ValueError, match="Invalid trial directory name"):
            parse_trial_path(path)

    def test_get_trial_status_success(self, tmp_path):
        """Test getting status for successful trial."""
        trial_dir = tmp_path / "trial-0"
        trial_dir.mkdir()
        (trial_dir / ".success").touch()

        assert get_trial_status(trial_dir) == "success"

    def test_get_trial_status_fail(self, tmp_path):
        """Test getting status for failed trial."""
        trial_dir = tmp_path / "trial-0"
        trial_dir.mkdir()
        (trial_dir / ".fail").touch()

        assert get_trial_status(trial_dir) == "fail"

    def test_get_trial_status_unknown(self, tmp_path):
        """Test getting status for trial with no marker."""
        trial_dir = tmp_path / "trial-0"
        trial_dir.mkdir()

        assert get_trial_status(trial_dir) == "unknown"


class TestConflictDetection:
    """Test conflict detection logic."""

    def test_detect_conflicts_none(self):
        """Test no conflicts when all trials have different identities."""
        trials = [
            TrialInfo(
                path=Path("/a/trial-0"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            ),
            TrialInfo(
                path=Path("/b/trial-1"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-1"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=1,
            ),
        ]

        conflicts = detect_conflicts(trials)
        assert len(conflicts) == 0

    def test_detect_conflicts_success_and_fail_ok(self):
        """Test no conflict when one success and one fail for same identity."""
        trials = [
            TrialInfo(
                path=Path("/a/trial-0"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            ),
            TrialInfo(
                path=Path("/b/trial-0"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="fail",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            ),
        ]

        conflicts = detect_conflicts(trials)
        assert len(conflicts) == 0

    def test_detect_conflicts_multiple_success(self):
        """Test conflict when multiple trials have success for same identity."""
        trials = [
            TrialInfo(
                path=Path("/a/trial-0"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            ),
            TrialInfo(
                path=Path("/b/trial-0"),
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            ),
        ]

        conflicts = detect_conflicts(trials)
        assert len(conflicts) == 1
        assert conflicts[0].identity == (
            "crs1",
            "bench1",
            "harness1",
            "bugfinding",
            "address",
            0,
        )
        assert len(conflicts[0].trials) == 2


class TestEnumerateTrials:
    """Test trial enumeration."""

    def test_enumerate_trials_basic(self, tmp_path):
        """Test basic trial enumeration."""
        # Create trial structure
        exp_data = tmp_path / "experiment-data"
        trial_dir = (
            exp_data
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()

        trials = enumerate_trials(exp_data)

        assert len(trials) == 1
        assert trials[0].crs == "crs1"
        assert trials[0].benchmark == "bench1"
        assert trials[0].harness == "harness1"
        assert trials[0].mode == "bugfinding"
        assert trials[0].sanitizer == "address"
        assert trials[0].trial_num == 0
        assert trials[0].status == "success"

    def test_enumerate_trials_multiple(self, tmp_path):
        """Test enumerating multiple trials."""
        exp_data = tmp_path / "experiment-data"

        # Trial 1
        trial1 = (
            exp_data
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
        )
        trial1.mkdir(parents=True)
        (trial1 / ".success").touch()

        # Trial 2
        trial2 = (
            exp_data
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-1"
        )
        trial2.mkdir(parents=True)
        (trial2 / ".fail").touch()

        # Trial 3 (different CRS)
        trial3 = (
            exp_data
            / "crs2"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
        )
        trial3.mkdir(parents=True)
        (trial3 / ".success").touch()

        trials = enumerate_trials(exp_data)

        assert len(trials) == 3
        statuses = {t.status for t in trials}
        assert statuses == {"success", "fail"}


class TestMergeTrials:
    """Test trial merging logic."""

    def test_merge_trials_success_only(self, tmp_path):
        """Test merging only includes successful trials."""
        # Create source trial
        src_dir = tmp_path / "src" / "experiment-data"
        trial_dir = (
            src_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()
        (trial_dir / "metadata.json").write_text('{"trial_num": 0}')

        # Create trial info
        trials = [
            TrialInfo(
                path=trial_dir,
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="success",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            )
        ]

        # Merge
        output_dir = tmp_path / "output" / "experiment-data"
        result = merge_trials(trials, output_dir)

        assert result.merged_count == 1
        assert result.skipped_count == 0
        assert (
            output_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
            / "metadata.json"
        ).exists()

    def test_merge_trials_skips_failed(self, tmp_path):
        """Test merging skips failed trials."""
        # Create source trial
        src_dir = tmp_path / "src" / "experiment-data"
        trial_dir = (
            src_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / ".fail").touch()

        # Create trial info
        trials = [
            TrialInfo(
                path=trial_dir,
                relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-0"),
                status="fail",
                crs="crs1",
                benchmark="bench1",
                harness="harness1",
                mode="bugfinding",
                sanitizer="address",
                trial_num=0,
            )
        ]

        # Merge
        output_dir = tmp_path / "output" / "experiment-data"
        result = merge_trials(trials, output_dir)

        assert result.merged_count == 0
        assert result.skipped_count == 1
        assert len(result.skipped_trials) == 1
