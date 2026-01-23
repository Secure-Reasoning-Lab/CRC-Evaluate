#!/usr/bin/env python3
"""Integration test for LiteLLM budget limit functionality.

This script tests that budget limits are correctly set when generating keys.

Usage:
    # Set environment variables first
    export LITELLM_BASE_URL="http://localhost:4000"
    export LITELLM_MASTER_KEY="sk-your-master-key"

    # Run with pytest
    uv run pytest tests/test_litellm_budget_integration.py -v -s

    # Or run directly with custom budget
    uv run python tests/test_litellm_budget_integration.py --budget 0.05
"""

import argparse
import sys

import pytest
from crsbench.evaluation.litellm_tracker import (
    LiteLLMTracker,
    LiteLLMTrackerError,
    is_tracking_available,
)


def test_budget_limit(budget: float) -> bool:
    """Test that budget limit is correctly set on key generation.

    Args:
        budget: Budget limit in USD to test with

    Returns:
        True if test passed, False otherwise
    """
    print(f"\n{'=' * 60}")
    print("LiteLLM Budget Limit Integration Test")
    print(f"{'=' * 60}\n")

    # Check if tracking is available
    if not is_tracking_available():
        print("ERROR: LiteLLM tracking not available.")
        print(
            "Please set LITELLM_BASE_URL and LITELLM_MASTER_KEY environment variables."
        )
        return False

    try:
        tracker = LiteLLMTracker()
        print(f"✓ Connected to LiteLLM at: {tracker.base_url}")
    except LiteLLMTrackerError as e:
        print(f"ERROR: Failed to initialize tracker: {e}")
        return False

    # Generate key with budget
    print(f"\n[1] Generating key with max_budget=${budget}...")
    try:
        api_key = tracker.generate_key(
            experiment="budget-test",
            crs="test-crs",
            benchmark="test-benchmark",
            harness="test-harness",
            trial_num=1,
            max_budget=budget,
        )
        print(f"✓ Generated key: {api_key[:20]}...")
    except LiteLLMTrackerError as e:
        print(f"ERROR: Failed to generate key: {e}")
        return False

    # Get key info and verify budget
    print("\n[2] Retrieving key info...")
    try:
        key_info = tracker.get_key_info(api_key)
        print("✓ Retrieved key info")

        # Extract info
        info = key_info.get("info", {})
        actual_budget = info.get("max_budget")
        spend = info.get("spend", 0)
        key_alias = info.get("key_alias", "unknown")

        print("\n[3] Key Details:")
        print(f"    - Key Alias: {key_alias}")
        print(f"    - Max Budget: {actual_budget}")
        print(f"    - Current Spend: {spend}")
        print(f"    - Expected Budget: {budget}")

        # Verify budget
        print("\n[4] Verifying budget...")
        if actual_budget is None:
            print("✗ FAIL: max_budget is None (not set)")
            print("\n  Raw key info response:")
            print(f"  {key_info}")
            success = False
        elif abs(actual_budget - budget) < 0.0001:
            print(f"✓ PASS: Budget correctly set to ${actual_budget}")
            success = True
        else:
            print("✗ FAIL: Budget mismatch!")
            print(f"    Expected: ${budget}")
            print(f"    Actual: ${actual_budget}")
            success = False

    except LiteLLMTrackerError as e:
        print(f"ERROR: Failed to get key info: {e}")
        success = False

    # Cleanup: delete the key
    print("\n[5] Cleaning up (deleting test key)...")
    try:
        deleted = tracker.delete_key(api_key)
        if deleted:
            print("✓ Key deleted successfully")
        else:
            print("⚠ Key deletion returned False (may already be deleted)")
    except LiteLLMTrackerError as e:
        print(f"⚠ Failed to delete key (non-critical): {e}")

    print(f"\n{'=' * 60}")
    if success:
        print("TEST PASSED ✓")
    else:
        print("TEST FAILED ✗")
    print(f"{'=' * 60}\n")

    return success


@pytest.mark.skipif(
    not is_tracking_available(),
    reason="LITELLM_BASE_URL and LITELLM_MASTER_KEY not set",
)
def test_budget_limit_integration():
    """Pytest integration test for budget limit."""
    assert test_budget_limit(0.05), "Budget limit test failed"


def main():
    parser = argparse.ArgumentParser(
        description="Test LiteLLM budget limit functionality"
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=0.05,
        help="Budget limit in USD to test with (default: 0.05)",
    )
    args = parser.parse_args()

    success = test_budget_limit(args.budget)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
