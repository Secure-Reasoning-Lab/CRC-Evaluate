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


def test_docs_do_not_use_removed_dataset_cli_forms() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    docs_root = repo_root / "docs"
    forbidden = [
        "crsbench dataset bundle",
        "crsbench dataset validate",
        "crsbench upload --dataset",
    ]

    offenders: list[str] = []
    for path in docs_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(repo_root)} :: {token}")

    assert not offenders, "Found removed CLI forms in docs:\n" + "\n".join(offenders)


def test_distributed_docs_do_not_reference_legacy_rq_version() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    distributed_docs = repo_root / "docs" / "design" / "distributed"
    forbidden = "RQ 1.11.1+"

    offenders: list[str] = []
    for path in distributed_docs.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, (
        f"Legacy RQ version marker '{forbidden}' found in: {', '.join(offenders)}"
    )


def test_docs_do_not_use_invalid_benchmark_cli_options() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    docs_root = repo_root / "docs"
    forbidden = [
        "--dataset-name",
        "benchmark upload ./benchmarks/",
        "benchmark bundle-all ./benchmarks/ --output-dir",
        "benchmark bundle ./libpng-vuln-001 --output",
        "benchmark prepare-delta --all",
    ]

    offenders: list[str] = []
    for path in docs_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(repo_root)} :: {token}")

    assert not offenders, (
        "Found invalid benchmark command options in docs:\n" + "\n".join(offenders)
    )
