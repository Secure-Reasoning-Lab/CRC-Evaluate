# Documentation Taxonomy and Canonical Map

This document defines the canonical documentation structure for CRSBench.
Use it when adding, moving, removing, or merging docs. It is the single
documentation-governance source of truth.

## Taxonomy

### 1. Overview
- Goal: Explain what CRSBench is and where to start.
- Canonical entries:
  - `README.md`
  - `docs/README.md`

### 2. Setup and Operations
- Goal: Bring users from clone to running experiments safely.
- Canonical entries:
  - `docs/getting-started/install.md`
  - `docs/getting-started/configuration.md`
  - `docs/getting-started/first-experiment.md`
  - `docs/guides/experiments/**`
  - `docs/guides/benchmark-ci/**`

### 3. Contributor Workflows
- Goal: Guide framework and benchmark development tasks.
- Canonical entries:
  - `CONTRIBUTING.md`
  - `docs/contributors/framework-developer-guide.md`
  - `docs/contributors/benchmark-developer-guide.md`
  - `docs/contributors/coding-standards.md`
  - `docs/contributors/manual-validation.md`
  - `docs/contributors/testing.md`

### 4. Architecture and Design Rationale
- Goal: Capture system intent, tradeoffs, and internals.
- Canonical entries:
  - `docs/design/README.md`
  - `docs/design/architecture.md`
  - `docs/design/**`

### 5. Module Reference
- Goal: Keep module-level operational guidance concise and discoverable.
- Canonical entries:
  - `docs/modules/README.md`
  - `docs/modules/**`

### 6. Specification and Data Contracts
- Goal: Define normative benchmark requirements and example schema data.
- Canonical entries:
  - `docs/RFC.md`
  - `docs/benchmark-suite-example.yaml`
  - `docs/experiment-config-distributed-example.yaml`
  - `docs/meta-example.yaml`

### 7. Governance
- Goal: Keep documentation policy visible but separate from reader workflows.
- Canonical entries:
  - `docs/governance/documentation-taxonomy.md`
  - `docs/governance/documentation-inventory.md`

## Canonical Source Map

| Topic | Canonical page | Non-canonical duplicates/pointers |
|---|---|---|
| Project overview and quick start | `README.md` | Adjacent package `README.md` files (keep local scope only) |
| Docs navigation | `docs/README.md` | Root README docs section (summary only) |
| Benchmark format requirements | `docs/RFC.md` | None |
| Install and environment setup | `docs/getting-started/README.md` | `docs/getting-started/install.md`, `docs/getting-started/configuration.md`, operational snippets in `README.md` |
| Experiment execution workflow | `docs/guides/experiments/README.md` | `docs/getting-started/first-experiment.md`, command examples in `README.md` |
| Module operational guidance | `docs/modules/README.md` | `docs/modules/**`, deep implementation detail in design docs |
| Architecture decisions and rationale | `docs/design/README.md` | `docs/design/architecture.md`, relevant `docs/design/**`, intro summaries in module docs |

## Placement Rules

### Root vs Grouped-Docs Policy

- Keep `docs/` root reserved for:
  - the docs navigation hub: `docs/README.md`
  - top-level normative specs: `docs/RFC.md`
  - high-value example/reference artifacts that are intended to be directly discoverable, such as `docs/*.yaml`
- Put prose documentation under grouped subdirectories by reader/task:
  - `docs/getting-started/`
  - `docs/guides/`
  - `docs/reference/`
  - `docs/contributors/`
  - `docs/governance/`
  - `docs/design/`
  - `docs/modules/`
- Do not add root-level moved-page shims or compatibility pointers.
- Do not create duplicate canonical ownership across a root doc and a grouped doc.

### General Placement Rules

1. Add new user-facing prose docs under the grouped `docs/**` subdirectories unless the file is a root-level hub, normative spec, or high-visibility example artifact.
2. Keep repository entry-point content in `README.md` or `CONTRIBUTING.md`; do not duplicate full workflows there.
3. Put architecture or implementation internals in `docs/design/`.
4. Put reader-facing operational docs in `docs/getting-started/`, `docs/guides/`, or `docs/reference/`.
5. Keep `docs/modules/` concise and secondary; do not use it as a parallel primary navigation tree.
6. Keep each topic owned by one canonical page.
7. Avoid copying large command blocks to multiple pages; link to canonical setup/workflow pages.

## Redundancy-Control Rules

1. If two pages have the same primary audience and same purpose, merge them.
2. If content is outdated and not needed for compatibility, remove it.
3. If overlap is intentional, keep one short summary and link to the canonical deep page.
4. Do not reintroduce root-level pointer pages for moved docs.

## Ownership and Update Triggers

- Root docs owner: repository maintainers.
- Design docs owners: maintainers touching corresponding subsystems.
- Module docs owners: module maintainers and contributors changing those modules.

Update docs in the same PR when any of the following changes happen:
- CLI command names/flags or execution flow changes
- benchmark format/schema or validation behavior changes
- architecture-level behavior changes
- required environment variables or service dependencies change

## Maintenance Checklist

When editing docs, ensure:
- the canonical page is updated first
- duplicate text is removed or converted to a pointer page
- navigation links in `README.md` and `docs/README.md` still route correctly
- commands and config snippets match current repository behavior

## Diverse Reviewer Lanes

For major doc changes, request review across these roles:
- Onboarding reviewer: verifies quick-start clarity for first-time users.
- Runtime/ops reviewer: validates setup and workflow correctness.
- Module maintainer reviewer: checks module docs against implementation reality.
- Architecture reviewer: checks intent, decisions, and tradeoffs.
- Security/compliance reviewer: checks data-handling and secret-management guidance.

These reviewer lanes are role-based and can be fulfilled by any qualified maintainer/contributor.
