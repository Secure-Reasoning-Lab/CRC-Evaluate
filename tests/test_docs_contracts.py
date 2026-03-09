"""Docs contract checks for documentation structure and local links."""

from __future__ import annotations

import re
from pathlib import Path


def test_canonical_experiment_docs_reference_existing_configs() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    doc_paths = [
        repo_root / "docs" / "getting-started" / "first-experiment.md",
        repo_root / "docs" / "guides" / "experiments" / "config-reference.md",
        repo_root / "docs" / "README.md",
    ]

    matches: set[str] = set()
    for doc_path in doc_paths:
        text = doc_path.read_text(encoding="utf-8")
        matches.update(re.findall(r"experiment-configs/[A-Za-z0-9_./-]+\.yaml", text))

    missing = [rel for rel in sorted(matches) if not (repo_root / rel).exists()]

    assert not missing, (
        f"Missing experiment-config files referenced in canonical docs: {missing}"
    )


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


def test_local_markdown_links_resolve() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    markdown_files = list((repo_root / "docs").rglob("*.md")) + [
        repo_root / "README.md",
        repo_root / "CONTRIBUTING.md",
        repo_root / "AGENTS.md",
        repo_root / "scripts" / "README.md",
        repo_root / "experiment-configs" / "README.md",
    ]

    pattern = re.compile(r"\[[^\]]+\]\(([^)#]+)")
    offenders: list[str] = []

    for path in markdown_files:
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            link = match.group(1)
            if not link or link.startswith(("http://", "https://", "mailto:")):
                continue
            target = (path.parent / link).resolve()
            if not target.exists():
                offenders.append(f"{path.relative_to(repo_root)} -> {link}")

    assert not offenders, "Found broken local markdown links:\n" + "\n".join(
        sorted(offenders)
    )


def test_legacy_pointer_pages_use_single_canonical_target() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pointer_pages = [
        repo_root / "docs" / "environment-setup.md",
        repo_root / "docs" / "environment-variables.md",
        repo_root / "docs" / "experiment-workflow.md",
        repo_root / "docs" / "framework-developer-guide.md",
        repo_root / "docs" / "benchmark-developer-guide.md",
        repo_root / "docs" / "testing-setup.md",
        repo_root / "docs" / "coding-standards.md",
        repo_root / "docs" / "manual-validation-guideline.md",
        repo_root / "docs" / "ossfuzz-crs-interface.md",
        repo_root / "docs" / "seed-corpus.md",
        repo_root / "docs" / "snapshot-examples.md",
        repo_root / "docs" / "logger-usage-guide.md",
        repo_root / "docs" / "documentation-taxonomy.md",
        repo_root / "docs" / "documentation-inventory.md",
        repo_root / "docs" / "documentation-maintenance.md",
        repo_root / "docs" / "modules" / "benchmark-ci.md",
        repo_root / "docs" / "RFC.md",
    ]

    target_pattern = re.compile(r"Canonical page:\s+\[([^\]]+)\]\(([^)]+)\)")
    offenders: list[str] = []

    for path in pointer_pages:
        text = path.read_text(encoding="utf-8")
        matches = target_pattern.findall(text)
        if len(matches) != 1:
            offenders.append(
                f"{path.relative_to(repo_root)} :: expected exactly one canonical target"
            )
            continue
        target = (path.parent / matches[0][1]).resolve()
        if not target.exists():
            offenders.append(
                f"{path.relative_to(repo_root)} :: canonical target missing ({matches[0][1]})"
            )

    assert not offenders, "Legacy pointer pages are inconsistent:\n" + "\n".join(
        offenders
    )
