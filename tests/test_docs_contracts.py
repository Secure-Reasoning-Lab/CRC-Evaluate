"""Docs contract checks for runnable config references."""

from __future__ import annotations

import re
from pathlib import Path


def test_experiment_workflow_config_paths_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "experiment-workflow.md"
    text = doc_path.read_text(encoding="utf-8")

    matches = re.findall(r"experiment-configs/[A-Za-z0-9_./-]+\.yaml", text)
    assert matches, "No experiment-config paths found in docs/experiment-workflow.md"

    missing = []
    for rel in sorted(set(matches)):
        if not (repo_root / rel).exists():
            missing.append(rel)

    assert not missing, f"Missing experiment-config files referenced in docs: {missing}"
