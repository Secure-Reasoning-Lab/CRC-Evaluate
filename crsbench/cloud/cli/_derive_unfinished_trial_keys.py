"""Derive unfinished trial keys from collected cloud experiment artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import load_experiment_config
from crsbench.experiment.trial_selection import (
    default_collected_experiment_path,
    default_selector_output_path,
    derive_unfinished_trial_keys_from_config,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def run_derive_unfinished_trial_keys(args: argparse.Namespace) -> int:
    """Derive unfinished trial keys and write them to a newline-delimited file."""
    config = load_experiment_config(Path(args.config))

    collected_root = (
        Path(args.from_path)
        if args.from_path is not None
        else default_collected_experiment_path(config)
    )
    output_path = (
        Path(args.output)
        if args.output is not None
        else default_selector_output_path(config.experiment)
    )

    derived = derive_unfinished_trial_keys_from_config(
        config,
        collected_root=collected_root,
        rerun_failed_trials=args.rerun_failed_trials,
    )

    selected_keys_text = "\n".join(derived.selected_keys)
    if selected_keys_text:
        selected_keys_text += "\n"
    output_path.write_text(selected_keys_text, encoding="utf-8")

    logger.info(
        "Derived unfinished trial keys: selected={}, finished_success={}, finished_fail={}",
        len(derived.selected_keys),
        len(derived.finished_success_keys),
        len(derived.finished_fail_keys),
    )
    return 0
