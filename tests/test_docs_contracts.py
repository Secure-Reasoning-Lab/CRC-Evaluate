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


def test_docs_do_not_reference_removed_legacy_doc_paths() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    markdown_files = list((repo_root / "docs").rglob("*.md")) + [
        repo_root / "README.md",
        repo_root / "CONTRIBUTING.md",
        repo_root / "AGENTS.md",
        repo_root / "scripts" / "README.md",
        repo_root / "experiment-configs" / "README.md",
    ]
    forbidden = [
        "docs/environment-setup.md",
        "docs/environment-variables.md",
        "docs/experiment-workflow.md",
        "docs/framework-developer-guide.md",
        "docs/benchmark-developer-guide.md",
        "docs/testing-setup.md",
        "docs/coding-standards.md",
        "docs/manual-validation-guideline.md",
        "docs/ossfuzz-crs-interface.md",
        "docs/seed-corpus.md",
        "docs/snapshot-examples.md",
        "docs/logger-usage-guide.md",
        "docs/documentation-taxonomy.md",
        "docs/documentation-inventory.md",
        "docs/documentation-maintenance.md",
        "docs/modules/benchmark-ci.md",
        "docs/reference/benchmark-rfc.md",
        "docs/benchmark-spec.md",
    ]

    offenders: list[str] = []
    for path in markdown_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(repo_root)} :: {token}")

    assert not offenders, "Found references to removed legacy doc paths:\n" + "\n".join(
        sorted(offenders)
    )


def test_docs_root_contains_only_allowed_canonical_files() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    docs_root = repo_root / "docs"
    allowed_files = {
        "README.md",
        "RFC.md",
        "benchmark-suite-example.yaml",
        "experiment-config-distributed-example.yaml",
        "meta-example.yaml",
    }
    offenders = [
        path.name
        for path in docs_root.iterdir()
        if path.is_file() and path.name not in allowed_files
    ]

    assert not offenders, "Unexpected root-level docs files:\n" + "\n".join(
        sorted(offenders)
    )


def test_module_docs_do_not_contain_primary_cli_tutorial_sections() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    module_docs = list((repo_root / "docs" / "modules").rglob("*.md"))
    forbidden_headings = [
        "## Quick Start",
        "## CLI Usage",
        "## Usage",
    ]
    offenders: list[str] = []
    for path in module_docs:
        text = path.read_text(encoding="utf-8")
        for heading in forbidden_headings:
            if heading in text:
                offenders.append(f"{path.relative_to(repo_root)} :: {heading}")

    assert not offenders, (
        "Module docs contain tutorial-style CLI sections:\n"
        + "\n".join(sorted(offenders))
    )


def test_design_docs_do_not_contain_runnable_python_or_bash_fences() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    exempt = {
        repo_root / "docs" / "design" / "doc-authoring-guidelines.md",
    }
    offenders: list[str] = []
    for path in (repo_root / "docs" / "design").rglob("*.md"):
        if path in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        if "```python" in text or "```bash" in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, (
        "Design docs contain runnable python/bash fences:\n"
        + "\n".join(sorted(offenders))
    )


def test_design_docs_do_not_contain_implementation_checklists() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    exempt = {
        repo_root / "docs" / "design" / "doc-authoring-guidelines.md",
    }
    offenders: list[str] = []
    for path in (repo_root / "docs" / "design").rglob("*.md"):
        if path in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        if "- [ ]" in text or "- [x]" in text:
            offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, (
        "Design docs contain implementation checklists:\n"
        + "\n".join(sorted(offenders))
    )
