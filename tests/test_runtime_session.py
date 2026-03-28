"""Tests for distributed runtime session bootstrapping."""

from unittest.mock import MagicMock, patch

import pytest
from crsbench.distributed.runtime_session import DistributedRuntimeSession


def test_for_reeval_wires_verify_and_patch_queues() -> None:
    verify_queue = MagicMock()
    build_queue = MagicMock()
    patch_verify_queue = MagicMock()
    redis_conn = MagicMock()

    with (
        patch(
            "crsbench.distributed.runtime_session.initialize_verify_queue",
            return_value=verify_queue,
        ),
        patch(
            "crsbench.distributed.runtime_session.initialize_patch_queues",
            return_value=(build_queue, patch_verify_queue),
        ),
        patch(
            "crsbench.distributed.runtime_session.create_redis_connection",
            return_value=redis_conn,
        ),
    ):
        session = DistributedRuntimeSession.for_reeval(
            redis_host="localhost", experiment_name="exp"
        )

    assert session is not None
    assert session.trial_queue is verify_queue
    assert session.build_queue is build_queue
    assert session.verify_queue is patch_verify_queue
    assert session.cloud_readiness is not None


def test_for_run_wires_cloud_readiness_store() -> None:
    trial_queue = MagicMock()
    redis_conn = MagicMock()
    trial_queue.connection = redis_conn

    with patch(
        "crsbench.distributed.runtime_session.initialize_queue",
        return_value=trial_queue,
    ):
        session = DistributedRuntimeSession.for_run(
            redis_host="localhost",
            experiment_name="exp",
        )

    assert session is not None
    assert session.trial_queue is trial_queue
    assert session.cloud_readiness is not None


def test_register_or_raise_on_lock_contention() -> None:
    from crsbench.distributed.runtime_session import LockContentionError

    session = DistributedRuntimeSession(
        experiment_name="exp",
        redis_host="localhost",
        redis_conn=MagicMock(),
        registry=MagicMock(),
        lease=MagicMock(),
    )
    session.lease.acquire_lock.return_value = False

    with pytest.raises(LockContentionError, match="already locked"):
        session.register_or_raise(MagicMock(experiment="exp"))


def test_ensure_registered_if_missing_skips_when_existing() -> None:
    session = DistributedRuntimeSession(
        experiment_name="exp",
        redis_host="localhost",
        redis_conn=MagicMock(),
        registry=MagicMock(),
        lease=MagicMock(),
    )
    session.registry.get_experiment.return_value = MagicMock()

    registered = session.ensure_registered_if_missing(MagicMock(experiment="exp"))

    assert registered is False
    session.lease.acquire_lock.assert_not_called()
    session.lease.register.assert_not_called()


def test_resume_or_raise_reconciles_without_started_monitor() -> None:
    session = DistributedRuntimeSession(
        experiment_name="exp",
        redis_host="localhost",
        redis_conn=MagicMock(),
        registry=MagicMock(),
        lease=MagicMock(),
        lifecycle_store=MagicMock(),
    )
    session.lease.try_resume_lock.return_value = True
    artifact_checker = MagicMock(return_value=None)
    monitor = MagicMock()
    monitor.reconcile_on_resume.return_value = ["job-syncing"]

    with patch(
        "crsbench.distributed.runtime_session.JobMonitorLoop",
        return_value=monitor,
    ) as monitor_cls:
        needs_collection = session.resume_or_raise(artifact_checker=artifact_checker)

    assert needs_collection == ["job-syncing"]
    monitor_cls.assert_called_once()
    monitor.reconcile_on_resume.assert_called_once_with()
