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
- Canonical section hubs:
  - `docs/getting-started/README.md`
  - `docs/guides/experiments/README.md`
  - `docs/guides/benchmark-ci/README.md`
- Canonical topic pages:
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

### 4. Module Reference
- Goal: Keep module-scoped reference and short operational notes concise and discoverable.
- Scope:
  - one subsystem at a time
  - terminology, file layout, and short operational notes for that subsystem
- Non-goals:
  - first-time-user onboarding
  - cross-module workflows
  - primary operator runbooks
  - deep architecture/design rationale
- Canonical entries:
  - `docs/modules/README.md`
  - `docs/modules/**`

### 5. Specification and Data Contracts
- Goal: Define normative benchmark requirements and example schema data.
- Canonical entries:
  - `docs/RFC.md`
  - `docs/benchmark-suite-example.yaml`
  - `docs/experiment-config-distributed-example.yaml`
  - `docs/meta-example.yaml`

### 6. Governance
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
| Getting-started section hub | `docs/getting-started/README.md` | Summary links in `docs/README.md`, root `README.md` |
| Install workflow | `docs/getting-started/install.md` | Setup summaries in `docs/getting-started/README.md`, root `README.md` |
| Environment and configuration | `docs/getting-started/configuration.md` | Setup summaries in `docs/getting-started/README.md`, root `README.md` |
| First experiment walkthrough | `docs/getting-started/first-experiment.md` | Workflow summaries in `docs/guides/experiments/README.md`, root `README.md` |
| Experiment workflow section hub | `docs/guides/experiments/README.md` | Command examples in `README.md` |
| Benchmark CI workflow section hub | `docs/guides/benchmark-ci/README.md` | Command examples in `README.md` |
| Module reference hub | `docs/modules/README.md` | Module-scoped notes in `docs/modules/**`; deeper workflow detail belongs in guides/reference pages |

## Placement Rules

### Root vs Grouped-Docs Policy

- Keep `docs/` root reserved for:
  - the docs navigation hub: `docs/README.md`
  - top-level normative specs: `docs/RFC.md`
  - repository-level schema/example artifacts:
    - `docs/*.yaml`
    - explicitly approved top-level example/reference docs only when they serve the whole repository rather than one workflow or subsystem
- Put prose documentation under grouped subdirectories by reader/task:
  - `docs/getting-started/`
  - `docs/guides/`
  - `docs/reference/`
  - `docs/contributors/`
  - `docs/governance/`
  - `docs/modules/`
- Treat `docs/modules/` as module-scoped reference and short operational notes only.
- Do not place first-time-user onboarding or primary cross-cutting workflows in `docs/modules/`; those belong in `docs/getting-started/` or `docs/guides/`.
- Do not add root-level moved-page shims or compatibility pointers.
- Do not create duplicate canonical ownership across a root doc and a grouped doc.
- Do not place ordinary workflow guides, contributor docs, subsystem references, or moved-page aliases at `docs/` root.

### General Placement Rules

1. Add new user-facing prose docs under the grouped `docs/**` subdirectories unless the file is a root-level hub, normative spec, or high-visibility example artifact.
2. Keep repository entry-point content in `README.md` or `CONTRIBUTING.md`; do not duplicate full workflows there.
3. Put reader-facing operational docs in `docs/getting-started/`, `docs/guides/`, or `docs/reference/`.
4. Keep module-scoped architecture notes in `docs/modules/` only when they are concise and tied to one subsystem.
5. Keep `docs/modules/` concise, module-scoped, and secondary; do not use it as a parallel primary navigation tree.
6. Keep each topic owned by one canonical page.
7. Avoid copying large command blocks to multiple pages; link to canonical setup/workflow pages.

## Redundancy-Control Rules

1. If two pages have the same primary audience and same purpose, merge them.
2. If content is outdated and not needed for compatibility, remove it.
3. If overlap is intentional, keep one short summary and link to the canonical deep page.
4. Do not reintroduce root-level pointer pages for moved docs.

## Ownership and Update Triggers

- Root docs owner: repository maintainers.
- Module docs owners: module maintainers and contributors changing those modules.
- Repository maintainers explicitly own `README.md`, `docs/README.md`, and this
  governance policy.

Update docs in the same PR when any of the following changes happen:
- CLI command names/flags or execution flow changes
- benchmark format/schema or validation behavior changes
- architecture-level behavior changes
- required environment variables or service dependencies change

If documentation and implementation disagree, implementation truth wins and the
docs must be corrected before merge.

## Maintenance Checklist

When editing docs, ensure:
- the canonical page is updated first
- duplicate text is removed or merged into the canonical page
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

For major doc changes:
- cover at least three reviewer lanes
- make reviewer-lane coverage explicit in review notes
- one qualified reviewer may cover multiple lanes, but the lane coverage must be explicit

## Recommended Review Batches

For large documentation migrations or reorganizations, review in this order:

1. structure and navigation
2. content refresh and de-duplication
3. intent and rationale quality
4. cleanup and validation
