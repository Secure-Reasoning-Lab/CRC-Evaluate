"""Tests for transported-config rewrite of inputs.pov.from_experiment."""

from __future__ import annotations

import base64
import textwrap
from typing import TYPE_CHECKING

import yaml
from crsbench.cloud.gce.metadata import (
    CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
    _render_transported_config_bytes,
    _rewrite_from_experiment_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "experiment.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_rewrite_replaces_absolute_path() -> None:
    config = yaml.safe_load(
        textwrap.dedent(
            """
            runtime:
              inputs:
                pov:
                  from_experiment: /home/user/.run/cc-finding
            """
        )
    )
    rewritten = _rewrite_from_experiment_path(
        config, "/var/lib/crsbench/from-experiment/exp-1"
    )
    assert (
        rewritten["runtime"]["inputs"]["pov"]["from_experiment"]
        == "/var/lib/crsbench/from-experiment/exp-1"
    )


def test_rewrite_replaces_relative_path() -> None:
    config = yaml.safe_load(
        textwrap.dedent(
            """
            runtime:
              inputs:
                pov:
                  from_experiment: .run/cc-finding
            """
        )
    )
    rewritten = _rewrite_from_experiment_path(
        config, "/var/lib/crsbench/from-experiment/exp-2"
    )
    assert (
        rewritten["runtime"]["inputs"]["pov"]["from_experiment"]
        == "/var/lib/crsbench/from-experiment/exp-2"
    )


def test_rewrite_noop_when_field_missing() -> None:
    config = yaml.safe_load(
        textwrap.dedent(
            """
            runtime:
              inputs:
                pov:
                  enabled: true
            """
        )
    )
    rewritten = _rewrite_from_experiment_path(
        config, "/var/lib/crsbench/from-experiment/exp-3"
    )
    assert "from_experiment" not in rewritten["runtime"]["inputs"]["pov"]


def test_rewrite_noop_when_runtime_missing() -> None:
    config = {"experiment": "exp-4"}
    rewritten = _rewrite_from_experiment_path(config, "/remote/path")
    assert rewritten == {"experiment": "exp-4"}


def test_render_transported_config_rewrites_path(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        experiment: exp-5
        runtime:
          inputs:
            pov:
              from_experiment: .run/cc-finding
          litellm:
            mode: external
        """,
    )

    encoded = _render_transported_config_bytes(
        config_path,
        from_experiment_remote_path="/var/lib/crsbench/from-experiment/exp-5",
    )

    parsed = yaml.safe_load(encoded)
    assert (
        parsed["runtime"]["inputs"]["pov"]["from_experiment"]
        == "/var/lib/crsbench/from-experiment/exp-5"
    )
    # Unrelated fields remain intact.
    assert parsed["runtime"]["litellm"]["mode"] == "external"
    assert parsed["experiment"] == "exp-5"


def test_render_transported_config_no_rewrite_when_none(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        experiment: exp-6
        runtime:
          inputs:
            pov:
              from_experiment: .run/cc-finding
        """,
    )

    encoded = _render_transported_config_bytes(
        config_path,
        from_experiment_remote_path=None,
    )

    parsed = yaml.safe_load(encoded)
    assert parsed["runtime"]["inputs"]["pov"]["from_experiment"] == ".run/cc-finding"


def test_build_orchestrator_metadata_rewrites_from_experiment(
    tmp_path: Path, monkeypatch
) -> None:
    from crsbench.cloud.gce import metadata as metadata_module

    config_path = _write_config(
        tmp_path,
        """
        experiment: exp-7
        runtime:
          inputs:
            pov:
              from_experiment: .run/cc-finding
        """,
    )

    # Stub out collateral helpers so we can exercise just the config-bytes path.
    class _FakeOrchestrator:
        metadata: dict[str, str] = {}

    monkeypatch.setattr(metadata_module, "_apply_access_metadata", lambda **_: None)
    monkeypatch.setattr(metadata_module, "_apply_install_metadata", lambda **_: None)
    monkeypatch.setattr(
        metadata_module, "_apply_startup_script_metadata", lambda **_: None
    )

    built = metadata_module.build_orchestrator_metadata(
        experiment_name="exp-7",
        orchestrator=_FakeOrchestrator(),
        experiment_config_path=str(config_path),
        redis_password="irrelevant",
        startup_script="#!/bin/sh\n",
        from_experiment_remote_path="/var/lib/crsbench/from-experiment/exp-7",
    )

    decoded = base64.b64decode(built[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY])
    parsed = yaml.safe_load(decoded)
    assert (
        parsed["runtime"]["inputs"]["pov"]["from_experiment"]
        == "/var/lib/crsbench/from-experiment/exp-7"
    )
