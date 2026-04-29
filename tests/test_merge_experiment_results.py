"""Tests for merge_experiment_results.py script."""

import json
from pathlib import Path

import pytest
from scripts import merge_experiment_results
from scripts.merge_experiment_results import (
    TrialInfo,
    detect_conflicts,
    enumerate_trials,
    get_trial_status,
    merge_trial_matrices,
    merge_trials,
    parse_trial_path,
)


class TestParsing:
    """Test path parsing functions."""

    def test_parse_trial_path_valid(self):
        """Test parsing valid trial path."""
        path = Path("crs1/bench1/harness1/bugfinding/address/trial-0")
        crs, benchmark, harness, cpv, mode, sanitizer, trial_num = parse_trial_path(
            path
        )

        assert crs == "crs1"
        assert benchmark == "bench1"
        assert harness == "harness1"
        assert cpv is None
        assert mode == "bugfinding"
        assert sanitizer == "address"
        assert trial_num == 0

    def test_parse_trial_path_valid_with_cpv(self):
        """Test parsing valid trial path with CPV directory."""
        path = Path("crs1/bench1/harness1/cpv_0/bugfinding/address/trial-0")
        crs, benchmark, harness, cpv, mode, sanitizer, trial_num = parse_trial_path(
            path
        )

        assert crs == "crs1"
        assert benchmark == "bench1"
        assert harness == "harness1"
        assert cpv == "cpv_0"
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
            None,
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

    def test_merge_trials_renumbers_duplicate_successful_trials(self, tmp_path):
        """Renumber mode should treat repeated trial-1 runs as distinct trials."""
        trials = []
        for source_index in (1, 2):
            src_dir = tmp_path / f"src{source_index}" / "experiment-data"
            trial_dir = (
                src_dir
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / "trial-1"
            )
            trial_dir.mkdir(parents=True)
            (trial_dir / ".success").touch()
            (trial_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "trial_num": 1,
                        "crs": "crs1",
                        "benchmark": "bench1",
                        "harness": "harness1",
                        "mode": "bugfinding",
                    }
                ),
                encoding="utf-8",
            )
            (trial_dir / "llm-usage.json").write_text(
                json.dumps(
                    {
                        "trial_id": "exp-crs1-bench1-trial1-abc",
                        "key_alias": "exp-crs1-bench1-trial1-abc",
                        "key_info": {
                            "key_alias": "exp-crs1-bench1-trial1-abc",
                            "metadata": {"trial_num": 1},
                        },
                        "raw_response": {
                            "info": {
                                "key_alias": "exp-crs1-bench1-trial1-abc",
                                "metadata": {"trial_num": 1},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            trials.append(
                TrialInfo(
                    path=trial_dir,
                    relative_path=Path(
                        "crs1/bench1/harness1/bugfinding/address/trial-1"
                    ),
                    status="success",
                    crs="crs1",
                    benchmark="bench1",
                    harness="harness1",
                    mode="bugfinding",
                    sanitizer="address",
                    trial_num=1,
                )
            )

        output_dir = tmp_path / "output" / "experiment-data"
        result = merge_trials(trials, output_dir, renumber_trials=True)

        assert result.merged_count == 2
        assert (
            output_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-1"
        ).exists()
        trial_2 = (
            output_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-2"
        )
        assert trial_2.exists()
        metadata = json.loads((trial_2 / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["trial_num"] == 2
        llm_usage = json.loads((trial_2 / "llm-usage.json").read_text(encoding="utf-8"))
        assert llm_usage["trial_id"] == "exp-crs1-bench1-trial2-abc"
        assert llm_usage["key_alias"] == "exp-crs1-bench1-trial2-abc"
        assert llm_usage["key_info"]["key_alias"] == "exp-crs1-bench1-trial2-abc"
        assert llm_usage["key_info"]["metadata"]["trial_num"] == 2
        assert (
            llm_usage["raw_response"]["info"]["key_alias"]
            == "exp-crs1-bench1-trial2-abc"
        )
        assert llm_usage["raw_response"]["info"]["metadata"]["trial_num"] == 2

    def test_merge_trials_renumbers_llm_trial_id_with_token_boundaries(self):
        """Renumbering trial IDs should not rewrite trial10 as trial20."""
        from scripts.merge_experiment_results import _renumber_trial_id

        assert (
            _renumber_trial_id("experiment-trial1-bench-trial10-trial-1", 1, 2)
            == "experiment-trial2-bench-trial10-trial-2"
        )

    def test_merge_trials_renumbers_cpv_trial_paths(self, tmp_path):
        """Renumber mode should preserve CPV layout while changing trial directory."""
        src_dir = tmp_path / "src" / "experiment-data"
        trial_dir = (
            src_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "cpv_0"
            / "bugfinding"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()
        (trial_dir / "metadata.json").write_text(
            json.dumps({"trial_num": 1}), encoding="utf-8"
        )
        trial = TrialInfo(
            path=trial_dir,
            relative_path=Path("crs1/bench1/harness1/cpv_0/bugfinding/address/trial-1"),
            status="success",
            crs="crs1",
            benchmark="bench1",
            harness="harness1",
            cpv="cpv_0",
            mode="bugfinding",
            sanitizer="address",
            trial_num=1,
        )

        output_dir = tmp_path / "output" / "experiment-data"
        merge_trials([trial], output_dir, renumber_trials=True)

        assert (
            output_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "cpv_0"
            / "bugfinding"
            / "address"
            / "trial-1"
        ).exists()

    def test_renumber_trials_uses_numeric_trial_order(self, tmp_path):
        """Renumber mode should process trial-2 before trial-10 within a source."""
        exp_data = tmp_path / "src" / "experiment-data"
        for trial_num in (1, 2, 10):
            trial_dir = (
                exp_data
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / f"trial-{trial_num}"
            )
            trial_dir.mkdir(parents=True)
            (trial_dir / ".success").touch()
            (trial_dir / "metadata.json").write_text(
                json.dumps({"trial_num": trial_num}), encoding="utf-8"
            )

        trials = enumerate_trials(exp_data)
        output_dir = tmp_path / "output" / "experiment-data"
        result = merge_trials(trials, output_dir, renumber_trials=True)

        assert result.merged_count == 3
        assert (
            result.trial_number_by_source_path[
                exp_data
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / "trial-2"
            ]
            == 2
        )
        assert (
            result.trial_number_by_source_path[
                exp_data
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / "trial-10"
            ]
            == 3
        )

    def test_merge_trial_matrices_renumbers_entries(self, tmp_path):
        """Merged trial_matrix.json should reflect the renumbered trial numbers."""
        assignments = {}
        source_dirs = []
        for source_index in (1, 2):
            exp_data = tmp_path / f"src{source_index}" / "experiment-data"
            exp_data.mkdir(parents=True)
            source_dirs.append(exp_data)
            matrix = {
                "experiment": f"run-{source_index}",
                "total_trials": 1,
                "trials": [
                    {
                        "crs": "crs1",
                        "benchmark": "bench1",
                        "benchmark_path": "/bench1",
                        "harness": "harness1",
                        "harness_path": "harness1.c",
                        "trial_num": 1,
                        "mode": "bugfinding",
                        "sanitizer": "address",
                        "target_cpv_id": None,
                    }
                ],
            }
            (exp_data / "trial_matrix.json").write_text(
                json.dumps(matrix), encoding="utf-8"
            )
            trial_path = (
                exp_data
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / "trial-1"
            )
            assignments[trial_path] = source_index

        output_dir = tmp_path / "output" / "experiment-data"
        merge_trial_matrices(
            source_dirs,
            output_dir,
            experiment_name="merged",
            trial_number_by_source_path=assignments,
        )

        merged = json.loads(
            (output_dir / "trial_matrix.json").read_text(encoding="utf-8")
        )
        assert merged["experiment"] == "merged"
        assert merged["total_trials"] == 2
        assert [entry["trial_num"] for entry in merged["trials"]] == [1, 2]

    def test_merge_trial_matrices_empty_assignment_writes_no_matrix(self, tmp_path):
        """An empty copied-trial assignment should not include skipped matrix entries."""
        exp_data = tmp_path / "src" / "experiment-data"
        exp_data.mkdir(parents=True)
        matrix = {
            "experiment": "run-1",
            "total_trials": 1,
            "trials": [
                {
                    "crs": "crs1",
                    "benchmark": "bench1",
                    "harness": "harness1",
                    "trial_num": 1,
                    "mode": "bugfinding",
                    "sanitizer": "address",
                }
            ],
        }
        (exp_data / "trial_matrix.json").write_text(
            json.dumps(matrix), encoding="utf-8"
        )

        output_dir = tmp_path / "output" / "experiment-data"
        result = merge_trial_matrices(
            [exp_data],
            output_dir,
            trial_number_by_source_path={},
        )

        assert result is None
        assert not (output_dir / "trial_matrix.json").exists()

    def test_merge_trials_skips_existing_destination_without_rewriting(self, tmp_path):
        """Existing destinations should not be counted or rewritten."""
        src_dir = tmp_path / "src" / "experiment-data"
        trial_dir = (
            src_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-1"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()
        (trial_dir / "metadata.json").write_text(
            json.dumps({"trial_num": 1}), encoding="utf-8"
        )
        trial = TrialInfo(
            path=trial_dir,
            relative_path=Path("crs1/bench1/harness1/bugfinding/address/trial-1"),
            status="success",
            crs="crs1",
            benchmark="bench1",
            harness="harness1",
            mode="bugfinding",
            sanitizer="address",
            trial_num=1,
        )
        existing_dest = (
            tmp_path
            / "output"
            / "experiment-data"
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-1"
        )
        existing_dest.mkdir(parents=True)
        (existing_dest / "metadata.json").write_text(
            json.dumps({"trial_num": 99}), encoding="utf-8"
        )

        result = merge_trials([trial], tmp_path / "output" / "experiment-data")

        assert result.merged_count == 0
        assert result.skipped_count == 1
        metadata = json.loads(
            (existing_dest / "metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["trial_num"] == 99

    def test_main_renumber_trials_allows_duplicate_successes(
        self, tmp_path, monkeypatch
    ):
        """CLI renumber mode should merge duplicate successful trial-1 inputs."""
        input_dirs = []
        for source_index in (1, 2):
            exp_data = tmp_path / f"src{source_index}" / "experiment-data"
            input_dirs.append(exp_data)
            trial_dir = (
                exp_data
                / "crs1"
                / "bench1"
                / "harness1"
                / "bugfinding"
                / "address"
                / "trial-1"
            )
            trial_dir.mkdir(parents=True)
            (trial_dir / ".success").touch()
            (trial_dir / "metadata.json").write_text(
                json.dumps({"trial_num": 1}), encoding="utf-8"
            )

        output_dir = tmp_path / "merged" / "experiment-data"
        monkeypatch.setattr(
            "sys.argv",
            [
                "merge_experiment_results.py",
                "--input-dirs",
                str(input_dirs[0]),
                str(input_dirs[1]),
                "--output-dir",
                str(output_dir),
                "--renumber-trials",
            ],
        )

        assert merge_experiment_results.main() == 0
        assert (
            output_dir
            / "crs1"
            / "bench1"
            / "harness1"
            / "bugfinding"
            / "address"
            / "trial-2"
            / "metadata.json"
        ).exists()
