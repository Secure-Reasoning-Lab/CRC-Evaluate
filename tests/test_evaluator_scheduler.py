from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from crsbench.distributed.evaluator_scheduler import (
    SCHEDULER_OWNER_KEY_META,
    FairSchedulerState,
    QueuedCandidate,
    build_scheduler_owner_key_for_ci_job,
    build_scheduler_owner_key_from_payload,
    choose_next_fair_candidate,
    choose_next_queue_class,
    should_adopt_scheduler_owner,
)


def _candidate(queue_name: str, job_id: str, owner_key: str) -> QueuedCandidate:
    return QueuedCandidate(
        queue_name=queue_name,
        job_id=job_id,
        owner_key=owner_key,
    )


def test_owner_key_prefers_trial_identity_when_present() -> None:
    owner = build_scheduler_owner_key_from_payload(
        {
            "experiment_name": "exp-42",
            "trial_id": "trial-007",
            "benchmark": "libpng",
            "harness": "fuzz_png_read",
        },
        fallback_job_id="verify-job-1",
        queue_name="crsbench_exp-42_verify",
    )

    assert owner == "trial::exp-42::trial-007"


def test_owner_key_falls_back_to_smallest_non_trial_work_unit() -> None:
    owner = build_scheduler_owner_key_for_ci_job(
        SimpleNamespace(
            job_id="build-single/libpng/address",
            job_type="build",
            benchmark_name="libpng",
            harness="fuzz_png_read",
            sanitizer="address",
            trial_id=None,
        ),
        experiment_name="exp-42",
    )

    assert owner == "unit::exp-42::libpng::fuzz_png_read::address::build"


def test_trial_owner_replaces_generic_owner_on_duplicate_reuse() -> None:
    assert should_adopt_scheduler_owner(
        existing_owner="unit::exp-42::libpng::build",
        new_owner="trial::exp-42::trial-008",
    )
    assert not should_adopt_scheduler_owner(
        existing_owner="trial::exp-42::trial-007",
        new_owner="trial::exp-42::trial-008",
    )


def test_choose_next_fair_candidate_rotates_between_trial_owners() -> None:
    state = FairSchedulerState()
    queue_candidates = [
        [
            _candidate("build-q-a", "job-a1", "trial::exp::trial-a"),
            _candidate("build-q-a", "job-a2", "trial::exp::trial-a"),
        ],
        [_candidate("build-q-b", "job-b1", "trial::exp::trial-b")],
    ]

    first = choose_next_fair_candidate(
        queue_candidates,
        queue_class="build",
        state=state,
    )

    queue_candidates[0] = queue_candidates[0][1:]

    second = choose_next_fair_candidate(
        queue_candidates,
        queue_class="build",
        state=state,
    )

    assert first is not None
    assert second is not None
    assert first.job_id == "job-a1"
    assert second.job_id == "job-b1"


def test_choose_next_fair_candidate_avoids_fixed_queue_name_bias() -> None:
    state = FairSchedulerState(
        next_queue_index_by_class={"build": 0, "verify": 1},
    )
    queue_candidates = [
        [_candidate("verify-q-a", "job-a1", "trial::exp::trial-a")],
        [_candidate("verify-q-b", "job-b1", "trial::exp::trial-b")],
    ]

    chosen = choose_next_fair_candidate(
        queue_candidates,
        queue_class="verify",
        state=state,
    )

    assert chosen is not None
    assert chosen.job_id == "job-b1"


def test_choose_next_queue_class_alternates_when_both_classes_are_runnable() -> None:
    state = FairSchedulerState(next_queue_class="build")

    first = choose_next_queue_class(
        build_has_capacity=True,
        verify_has_capacity=True,
        build_has_work=True,
        verify_has_work=True,
        state=state,
    )
    second = choose_next_queue_class(
        build_has_capacity=True,
        verify_has_capacity=True,
        build_has_work=True,
        verify_has_work=True,
        state=state,
    )

    assert first == "build"
    assert second == "verify"


def test_choose_next_queue_class_falls_back_to_only_eligible_class() -> None:
    state = FairSchedulerState(next_queue_class="verify")

    chosen = choose_next_queue_class(
        build_has_capacity=True,
        verify_has_capacity=False,
        build_has_work=True,
        verify_has_work=True,
        state=state,
    )

    assert chosen == "build"


def test_enqueue_ci_job_adds_scheduler_owner_meta() -> None:
    from crsbench.distributed.verify_queue import enqueue_ci_job

    queue = MagicMock()
    queue.enqueue.return_value = MagicMock()

    with patch(
        "crsbench.distributed.ci_jobs.serialize_ci_job",
        return_value={"benchmark_name": "libpng"},
    ):
        enqueue_ci_job(
            queue,
            "exp-42",
            SimpleNamespace(
                job_id="build-single/libpng/address",
                job_type="build",
                benchmark_name="libpng",
                harness="fuzz_png_read",
                sanitizer="address",
                trial_id="trial-9",
            ),
        )

    assert (
        queue.enqueue.call_args.kwargs["meta"][SCHEDULER_OWNER_KEY_META]
        == "trial::exp-42::trial-9"
    )


def test_enqueue_single_pov_adds_scheduler_owner_meta() -> None:
    from crsbench.distributed.verify_queue import enqueue_single_pov

    verify_queue = MagicMock()
    verify_queue.enqueue.return_value = MagicMock(id="verify-job-1")

    enqueue_single_pov(
        verify_queue,
        "exp-42",
        "trial-12",
        "libpng",
        "fuzz_png_read",
        "pov_0",
        b"boom",
    )

    assert (
        verify_queue.enqueue.call_args.kwargs["meta"][SCHEDULER_OWNER_KEY_META]
        == "trial::exp-42::trial-12"
    )


@patch("crsbench.distributed.verify_queue.rq")
def test_duplicate_reuse_adopts_trial_owner_metadata(mock_rq: MagicMock) -> None:
    from crsbench.distributed.verify_queue import _enqueue_with_existing_reuse

    queue = MagicMock()
    queue.connection = MagicMock()
    queue.enqueue.side_effect = RuntimeError("job id already exists")

    existing_job = MagicMock()
    existing_job.meta = {
        SCHEDULER_OWNER_KEY_META: "unit::exp-42::libpng::fuzz_png_read::build"
    }
    mock_rq.job.Job.fetch.return_value = existing_job

    reused = _enqueue_with_existing_reuse(
        queue,
        "crsbench.distributed.ci_jobs.execute_ci_job",
        {"trial_id": "trial-12", "benchmark": "libpng"},
        job_timeout=3600,
        meta={
            SCHEDULER_OWNER_KEY_META: "trial::exp-42::trial-12",
            "experiment_name": "exp-42",
        },
        job_id="build-single/libpng/address",
    )

    assert reused is existing_job
    assert existing_job.meta[SCHEDULER_OWNER_KEY_META] == "trial::exp-42::trial-12"
    existing_job.save_meta.assert_called_once()
