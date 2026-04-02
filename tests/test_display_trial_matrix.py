"""Unit tests for display_trial_matrix with Order column."""

import re
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from crsbench.run_experiment import Trial, display_trial_matrix
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
        crs=f"crs_{trial_id % 3}",
        benchmark_harness=benchmark_harness,
        trial_num=trial_id,
        mode="delta",
        sanitizer="address",
    )


def extract_order_numbers(output: str) -> list[int]:
    """Extract order numbers from table output (works for both Rich and basic formats).

    Args:
        output: The captured output string

    Returns:
        List of order numbers found in the output
    """
    # Pattern to match order numbers in both formats:
    # - Basic: "1      crs_0  ..."
    # - Rich:  "│     1 │ crs_0 │ ..."
    # We look for lines that contain the trial data (crs_, benchmark_, harness_)
    # and extract the first number on that line
    order_numbers = []
    for line in output.split("\n"):
        # Skip header, separator, and empty lines
        if (
            not line
            or "Order" in line
            or "Trials to be enqueued" in line
            or "Total trials" in line
        ):
            continue
        # Skip Rich table borders
        if line.startswith(("┏", "┡", "┌", "└", "─", "-")):
            continue

        # Look for lines containing trial data
        if "crs_" in line and "benchmark_" in line:
            # Extract the first number from the line (the order number)
            match = re.search(r"\d+", line)
            if match:
                order_numbers.append(int(match.group()))

    return order_numbers


class TestDisplayTrialMatrix:
    """Test display_trial_matrix function."""

    def test_display_without_split(self):
        """Test display shows Order starting from 1."""
        trials = [create_mock_trial(i) for i in range(3)]

        # Capture stdout
        captured_output = StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            display_trial_matrix(trials, start_index=0)
        finally:
            sys.stdout = original_stdout

        output = captured_output.getvalue()

        # Verify Order column header is present
        assert "Order" in output

        # Verify Order numbers 1, 2, 3 are in the output
        order_numbers = extract_order_numbers(output)
        assert order_numbers == [1, 2, 3]

    def test_display_with_split_first_half(self):
        """Test display shows Order 1-5 for first split of 10 trials."""
        trials = [create_mock_trial(i) for i in range(5)]

        # Simulate first half of a 10-trial split (start_index=0)
        captured_output = StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            display_trial_matrix(trials, start_index=0)
        finally:
            sys.stdout = original_stdout

        output = captured_output.getvalue()

        # Verify Order numbers 1-5
        order_numbers = extract_order_numbers(output)
        assert order_numbers == [1, 2, 3, 4, 5]

    def test_display_with_split_second_half(self):
        """Test display shows Order 6-10 for second split of 10 trials."""
        trials = [create_mock_trial(i) for i in range(5, 10)]

        # Simulate second half of a 10-trial split (start_index=5)
        captured_output = StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            display_trial_matrix(trials, start_index=5)
        finally:
            sys.stdout = original_stdout

        output = captured_output.getvalue()

        # Verify Order numbers 6-10
        order_numbers = extract_order_numbers(output)
        assert order_numbers == [6, 7, 8, 9, 10]

    def test_empty_trials(self):
        """Test display handles empty trials list."""
        captured_output = StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            display_trial_matrix([], start_index=0)
        finally:
            sys.stdout = original_stdout

        output = captured_output.getvalue()
        assert "No trials to display" in output

    def test_uses_basic_output_when_rich_is_installed_but_stdout_is_not_tty(self):
        """Auto Rich selection should require an interactive stdout."""
        trials = [create_mock_trial(1)]

        with (
            patch(
                "crsbench.run_experiment.importlib.util.find_spec",
                return_value=object(),
            ),
            patch("sys.stdout.isatty", return_value=False),
            patch("crsbench.run_experiment._display_trial_matrix_basic") as basic,
            patch("crsbench.run_experiment._display_trial_matrix_rich") as rich,
        ):
            display_trial_matrix(trials, start_index=0)

        basic.assert_called_once_with(trials, 0)
        rich.assert_not_called()
