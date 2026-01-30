"""Unit tests for --split argument and job slicing functionality."""

from pathlib import Path

import pytest
from crsbench.run_experiment import (
    Trial,
    _calculate_split_start,
    apply_split_to_trials,
    parse_split_argument,
)
from crsbench.validation.schemas import BenchmarkHarness, HarnessFile


def create_mock_trial(trial_id: int) -> Trial:
    """Create a mock Trial object for testing.

    Args:
        trial_id: Unique ID for the trial

    Returns:
        Mock Trial object
    """
    harness_file = HarnessFile(
        name=f"harness_{trial_id}.cpp",
        path=f"/path/to/harness_{trial_id}.cpp",
    )
    benchmark_harness = BenchmarkHarness(
        name=f"benchmark_{trial_id}",
        path=Path(f"/path/to/benchmark_{trial_id}"),
        harness=harness_file,
    )
    return Trial(
        crs=f"crs_{trial_id % 3}",  # Cycle through 3 CRS types
        benchmark_harness=benchmark_harness,
        trial_num=trial_id,
        mode="delta",
        sanitizer="address",
    )


class TestParseSplitArgument:
    """Test parse_split_argument function."""

    def test_valid_formats(self):
        """Test valid split argument formats."""
        assert parse_split_argument("1/2") == (1, 2)
        assert parse_split_argument("2/2") == (2, 2)
        assert parse_split_argument("1/3") == (1, 3)
        assert parse_split_argument("3/3") == (3, 3)
        assert parse_split_argument("1/1") == (1, 1)
        assert parse_split_argument("5/10") == (5, 10)

    def test_invalid_format_missing_slash(self):
        """Test invalid format without slash."""
        with pytest.raises(ValueError, match="Expected A/N"):
            parse_split_argument("1")

    def test_invalid_format_too_many_slashes(self):
        """Test invalid format with too many slashes."""
        with pytest.raises(ValueError, match="Expected A/N"):
            parse_split_argument("1/2/3")

    def test_invalid_format_non_integers(self):
        """Test invalid format with non-integer values."""
        with pytest.raises(ValueError, match="must be integers"):
            parse_split_argument("a/b")

        with pytest.raises(ValueError, match="must be integers"):
            parse_split_argument("1.5/2")

    def test_invalid_total_slices(self):
        """Test invalid total_slices values."""
        with pytest.raises(ValueError, match="must be >= 1"):
            parse_split_argument("1/0")

        with pytest.raises(ValueError, match="must be >= 1"):
            parse_split_argument("1/-1")

    def test_invalid_slice_index_too_low(self):
        """Test slice_index < 1."""
        with pytest.raises(ValueError, match="between 1 and"):
            parse_split_argument("0/2")

    def test_invalid_slice_index_too_high(self):
        """Test slice_index > total_slices."""
        with pytest.raises(ValueError, match="between 1 and"):
            parse_split_argument("3/2")


class TestApplySplitToTrials:
    """Test apply_split_to_trials function."""

    def test_single_slice_returns_all(self):
        """Test single slice (no split) returns all trials."""
        trials = [create_mock_trial(i) for i in range(10)]
        result = apply_split_to_trials(trials, 1, 1)
        assert len(result) == 10
        assert result == trials

    def test_even_split_two_slices(self):
        """Test even split into 2 slices."""
        trials = [create_mock_trial(i) for i in range(10)]

        slice1 = apply_split_to_trials(trials, 1, 2)
        slice2 = apply_split_to_trials(trials, 2, 2)

        assert len(slice1) == 5
        assert len(slice2) == 5
        assert slice1[0].trial_num == 0
        assert slice1[-1].trial_num == 4
        assert slice2[0].trial_num == 5
        assert slice2[-1].trial_num == 9

    def test_uneven_split_three_slices(self):
        """Test uneven split into 3 slices."""
        trials = [create_mock_trial(i) for i in range(10)]

        slice1 = apply_split_to_trials(trials, 1, 3)
        slice2 = apply_split_to_trials(trials, 2, 3)
        slice3 = apply_split_to_trials(trials, 3, 3)

        # First slice gets extra job (10 % 3 = 1 remainder)
        assert len(slice1) == 4  # 10 // 3 + 1
        assert len(slice2) == 3  # 10 // 3
        assert len(slice3) == 3  # 10 // 3

        # Verify contiguous ranges
        assert slice1[0].trial_num == 0
        assert slice1[-1].trial_num == 3
        assert slice2[0].trial_num == 4
        assert slice2[-1].trial_num == 6
        assert slice3[0].trial_num == 7
        assert slice3[-1].trial_num == 9

    def test_union_coverage(self):
        """Test that union of all slices equals original trials."""
        trials = [create_mock_trial(i) for i in range(10)]

        # Test 2 slices
        slice1 = apply_split_to_trials(trials, 1, 2)
        slice2 = apply_split_to_trials(trials, 2, 2)
        combined = slice1 + slice2
        assert len(combined) == len(trials)
        assert {t.trial_num for t in combined} == {t.trial_num for t in trials}

        # Test 3 slices
        slice1 = apply_split_to_trials(trials, 1, 3)
        slice2 = apply_split_to_trials(trials, 2, 3)
        slice3 = apply_split_to_trials(trials, 3, 3)
        combined = slice1 + slice2 + slice3
        assert len(combined) == len(trials)
        assert {t.trial_num for t in combined} == {t.trial_num for t in trials}

    def test_contiguity(self):
        """Test that each slice is a contiguous range."""
        trials = [create_mock_trial(i) for i in range(20)]

        for total_slices in [2, 3, 4, 5]:
            all_trial_nums = []
            for slice_index in range(1, total_slices + 1):
                slice_trials = apply_split_to_trials(trials, slice_index, total_slices)
                trial_nums = [t.trial_num for t in slice_trials]

                # Check contiguity: should be consecutive numbers
                assert trial_nums == list(range(trial_nums[0], trial_nums[-1] + 1))

                all_trial_nums.extend(trial_nums)

            # Check complete coverage
            assert sorted(all_trial_nums) == list(range(20))

    def test_edge_case_one_trial(self):
        """Test edge case with single trial."""
        trials = [create_mock_trial(0)]

        # Single slice
        result = apply_split_to_trials(trials, 1, 1)
        assert len(result) == 1
        assert result[0].trial_num == 0

        # Split into 2 (first slice gets it, second is empty)
        slice1 = apply_split_to_trials(trials, 1, 2)
        slice2 = apply_split_to_trials(trials, 2, 2)
        assert len(slice1) == 1
        assert len(slice2) == 0

    def test_edge_case_fewer_trials_than_slices(self):
        """Test edge case with fewer trials than requested slices."""
        trials = [create_mock_trial(i) for i in range(3)]

        # Split into 5 slices
        slice1 = apply_split_to_trials(trials, 1, 5)
        slice2 = apply_split_to_trials(trials, 2, 5)
        slice3 = apply_split_to_trials(trials, 3, 5)
        slice4 = apply_split_to_trials(trials, 4, 5)
        slice5 = apply_split_to_trials(trials, 5, 5)

        # First 3 slices get 1 trial each, last 2 are empty
        assert len(slice1) == 1
        assert len(slice2) == 1
        assert len(slice3) == 1
        assert len(slice4) == 0
        assert len(slice5) == 0

        # Union still covers all trials
        combined = slice1 + slice2 + slice3 + slice4 + slice5
        assert len(combined) == 3
        assert {t.trial_num for t in combined} == {0, 1, 2}

    def test_size_distribution(self):
        """Test that slice sizes differ by at most 1."""
        for n in [10, 15, 20, 100]:
            trials = [create_mock_trial(i) for i in range(n)]

            for total_slices in [2, 3, 4, 5, 7]:
                sizes = []
                for slice_index in range(1, total_slices + 1):
                    slice_trials = apply_split_to_trials(
                        trials, slice_index, total_slices
                    )
                    sizes.append(len(slice_trials))

                # Sizes should differ by at most 1
                assert max(sizes) - min(sizes) <= 1, (
                    f"Size distribution failed for n={n}, slices={total_slices}: "
                    f"sizes={sizes}"
                )

                # Sum should equal total
                assert sum(sizes) == n


class TestCalculateSplitStart:
    """Test _calculate_split_start function."""

    def test_single_slice_returns_zero(self):
        """Test single slice (no split) returns start index 0."""
        assert _calculate_split_start(10, 1, 1) == 0
        assert _calculate_split_start(100, 1, 1) == 0

    def test_even_split_two_slices(self):
        """Test even split into 2 slices."""
        # 10 trials: first 5, second 5
        assert _calculate_split_start(10, 1, 2) == 0
        assert _calculate_split_start(10, 2, 2) == 5

    def test_uneven_split_three_slices(self):
        """Test uneven split into 3 slices."""
        # 10 trials: 4, 3, 3 (first slice gets remainder)
        assert _calculate_split_start(10, 1, 3) == 0
        assert _calculate_split_start(10, 2, 3) == 4
        assert _calculate_split_start(10, 3, 3) == 7

    def test_consistency_with_apply_split(self):
        """Test that _calculate_split_start is consistent with apply_split_to_trials."""
        trials = [create_mock_trial(i) for i in range(20)]

        for total_slices in [2, 3, 4, 5]:
            for slice_index in range(1, total_slices + 1):
                start_idx = _calculate_split_start(
                    len(trials), slice_index, total_slices
                )
                slice_trials = apply_split_to_trials(trials, slice_index, total_slices)

                # The first trial in the slice should have trial_num == start_idx
                if slice_trials:
                    assert slice_trials[0].trial_num == start_idx

    def test_edge_case_fewer_trials_than_slices(self):
        """Test edge case with fewer trials than requested slices."""
        # 3 trials split into 5 slices
        assert _calculate_split_start(3, 1, 5) == 0
        assert _calculate_split_start(3, 2, 5) == 1
        assert _calculate_split_start(3, 3, 5) == 2
        assert _calculate_split_start(3, 4, 5) == 3  # Empty slice
        assert _calculate_split_start(3, 5, 5) == 3  # Empty slice
