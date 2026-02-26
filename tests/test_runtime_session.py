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
