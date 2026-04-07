"""Pytest configuration and fixtures."""

from pathlib import Path
from typing import Optional

import pytest
from dotenv import load_dotenv

# Load .env file from project root if it exists
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    load_dotenv(env_file)

_NOTIFICATION_ENV_VARS = (
    "CRSBENCH_NOTIFY_APPRISE_URLS",
    "CRSBENCH_NOTIFY_APPRISE_TITLE",
    "CRSBENCH_NOTIFY_APPRISE_TAG",
)

# These distributed orchestration regressions traverse the real notification-capable
# paths (enqueue, monitor, bring-up, resume). Keep them under the `notification`
# marker so `scripts/ci-tests/run-local.sh checks` can exclude them with
# `-m "not notification"` and avoid emitting operator alerts.
_FORCED_NOTIFICATION_TESTS = frozenset(
    {
        "test_register_failure_cleans_registry_lease",
        "test_existing_jobs_non_interactive_defaults_to_continue",
        "test_continue_mode_does_not_retry_failed_by_default",
        "test_continue_mode_retry_failed_requeues",
        "test_continue_mode_retry_failed_revives_lifecycle_before_requeue",
        "test_continue_mode_retry_failed_rolls_back_lifecycle_when_requeue_fails",
        "test_continue_mode_retry_failed_rolls_back_lifecycle_when_save_meta_fails",
        "test_continue_mode_retry_failed_skips_when_lifecycle_is_already_completed",
        "test_continue_mode_monitors_existing_finished_jobs_without_reenqueue",
        "test_continue_mode_monitors_existing_terminal_jobs_alongside_new_enqueues",
        "test_queue_mode_quit_exits_without_registration",
        "test_continue_mode_lock_contention_skips_queue_mutations",
        "test_continue_mode_reclaims_stale_lock_and_continues_recovery",
        "test_continue_mode_monitors_resume_collection_jobs_before_early_exit",
        "test_continue_mode_reconciles_resume_state_after_fresh_lock_acquisition",
        "test_continue_mode_skips_resume_collection_job_when_active_retry_exists",
        "test_queue_mode_fresh_acquires_lock_before_clearing",
        "test_queue_mode_fresh_preserves_lifecycle_when_started_job_was_purged",
        "test_queue_mode_fresh_lifecycle_clear_is_best_effort",
        "test_queue_mode_fresh_clears_lifecycle_for_stale_started_job",
        "test_queue_mode_fresh_clears_lifecycle_for_non_started_registry_residue",
        "test_distributed_enqueue_uses_deterministic_trial_job_id",
        "test_cloud_fleet_bringup_runs_before_enqueue",
        "test_provider_neutral_cloud_workers_validate_quota_before_bringup",
        "test_provider_neutral_cloud_workers_seed_lifecycle_and_start_monitor",
        "test_provider_neutral_cloud_retry_failed_refreshes_active_existing_jobs",
        "test_provider_neutral_cloud_workers_resolve_secret_refs_before_bringup",
        "test_provider_neutral_cloud_workers_pass_layered_env_payloads",
        "test_provider_neutral_cloud_instances_with_evaluators_pass_layered_env_payloads",
        "test_provider_neutral_preprovisioned_observe_does_not_resolve_secret_refs_again",
        "test_provider_neutral_preprovisioned_evaluators_use_combined_observe",
        "test_cloud_fleet_failure_aborts_before_enqueue",
        "test_cloud_fleet_bringup_is_skipped_when_no_trials_remain",
        "test_cloud_fleet_bringup_is_skipped_for_preprovisioned_remote_orchestrator",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag distributed notification-capable regressions consistently."""
    for item in items:
        original_name = getattr(item, "originalname", item.name)
        if (
            original_name in _FORCED_NOTIFICATION_TESTS
            and "notification" not in item.keywords
        ):
            item.add_marker(pytest.mark.notification)


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate OssCrsAdapter lock/sentinel files per test.

    Without this, parallel pytest-xdist workers sharing /tmp can leak
    prepare-done sentinel files across tests, causing false cache hits.
    """
    monkeypatch.setenv("CRSBENCH_OSS_CRS_BUILD_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _strip_live_notification_env_for_non_notification_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent routine tests from inheriting live Apprise targets from .env."""
    if request.node.get_closest_marker("notification") is not None:
        return
    for env_var in _NOTIFICATION_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def make_variant_name(
    benchmark: str,
    suffix: str,
    sanitizer: str = "address",
) -> str:
    """Generate variant name with sanitizer (new naming convention).

    Since multi-sanitizer support, variant names include sanitizer to prevent
    naming conflicts when building same variant with different sanitizers.

    Naming convention:
    - Format: {benchmark}-{san_short}-{suffix}
    - Examples: "bench-asan-deltaref", "bench-ubsan-delta-cpv0"

    Args:
        benchmark: Benchmark name (e.g., "test-proj")
        suffix: Variant suffix (e.g., "deltaref", "delta-cpv0")
        sanitizer: Full sanitizer name (default: "address")

    Returns:
        Full variant name (e.g., "test-proj-asan-deltaref")
    """
    # Map sanitizer to short name (consistent with builder.types.sanitizer_short_name)
    san_short_map = {
        "address": "asan",
        "undefined": "ubsan",
        "memory": "msan",
        "thread": "tsan",
        "coverage": "coverage",
    }
    san_short = san_short_map.get(sanitizer, sanitizer)
    return f"{benchmark}-{san_short}-{suffix}"


def make_job_id(
    job_type: str,
    benchmark: str,
    variant_suffix: Optional[str] = None,
    sanitizer: str = "address",
    extra: Optional[str] = None,
) -> str:
    """Generate job ID with variant name including sanitizer.

    Args:
        job_type: Job type (e.g., "build-single", "verify-cpv-pov")
        benchmark: Benchmark name
        variant_suffix: Variant suffix if applicable
        sanitizer: Full sanitizer name (default: "address")
        extra: Extra components for job ID (e.g., ":cpv_0")

    Returns:
        Job ID (e.g., "build-single:bench:bench-asan-deltaref")
    """
    if variant_suffix:
        variant_name = make_variant_name(benchmark, variant_suffix, sanitizer)
        job_id = f"{job_type}:{benchmark}:{variant_name}"
    else:
        job_id = f"{job_type}:{benchmark}"

    if extra:
        job_id = f"{job_id}{extra}"

    return job_id
