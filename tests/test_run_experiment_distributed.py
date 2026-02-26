"""Regression tests for distributed run orchestration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.run_experiment import run_experiment_distributed


def test_register_failure_cleans_registry_lease(tmp_path: Path) -> None:
    """If registration publish fails, lease cleanup should still run."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.crs_configs_dir = tmp_path
    config.resources = None
    config.keep_only_results = False

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = RuntimeError("register failed")

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            run_experiment_distributed("exp-test", config, [])

    session.cleanup.assert_called_once()
