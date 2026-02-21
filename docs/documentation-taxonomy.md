# Documentation Taxonomy and Canonical Map

This document defines the canonical documentation structure for CRSBench.
Use it when adding, moving, or removing docs.

## Taxonomy

### 1. Overview
- Goal: Explain what CRSBench is and where to start.
- Canonical entries:
  - `README.md`
  - `docs/README.md`

### 2. Setup and Operations
- Goal: Bring users from clone to running experiments safely.
- Canonical entries:
  - `docs/environment-setup.md`
  - `docs/experiment-workflow.md`
  - `docs/testing-setup.md`
  - `docs/ossfuzz-crs-interface.md`

### 3. Contributor Workflows
- Goal: Guide framework and benchmark development tasks.
- Canonical entries:
  - `CONTRIBUTING.md`
  - `docs/framework-developer-guide.md`
  - `docs/benchmark-developer-guide.md`
  - `docs/coding-standards.md`
  - `docs/manual-validation-guideline.md`

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
  - `docs/benchmark-spec.md` (compatibility pointer to RFC)
  - `docs/benchmark-suite-example.yaml`
  - `docs/experiment-config-example.yaml`
  - `docs/experiment-config-distributed-example.yaml`
  - `docs/meta-example.yaml`

## Canonical Source Map

| Topic | Canonical page | Non-canonical duplicates/pointers |
|---|---|---|
| Project overview and quick start | `README.md` | Adjacent package `README.md` files (keep local scope only) |
| Docs navigation | `docs/README.md` | Root README docs section (summary only) |
| Benchmark format requirements | `docs/RFC.md` | `docs/benchmark-spec.md` (pointer only) |
| Environment variables and service setup | `docs/environment-setup.md` | Operational snippets in `README.md` |
| Distributed runtime workflow | `docs/experiment-workflow.md` | Command examples in `README.md` |
| Module operational guidance | `docs/modules/**` | Deep implementation detail in design docs |
| Architecture decisions and rationale | `docs/design/architecture.md` + relevant `docs/design/**` pages | Intro summaries in module docs |

## Placement Rules

1. Add new top-level project behavior docs under `docs/` unless they are repository entry-point content for `README.md` or `CONTRIBUTING.md`.
2. Put architecture or implementation internals in `docs/design/`.
3. Put module-facing operational docs in `docs/modules/`, then link to design docs for details.
4. Keep each topic owned by one canonical page; other pages should point to it.
5. Avoid copying large command blocks to multiple pages; link to canonical setup/workflow pages.

## Redundancy-Control Rules

1. If two pages have the same primary audience and same purpose, merge them.
2. If one page is needed for backward compatibility, convert it to a pointer page.
3. If content is outdated and not needed for compatibility, remove it.
4. If overlap is intentional, keep one short summary and link to the canonical deep page.

## Ownership and Update Triggers

- Root docs owner: repository maintainers.
- Design docs owners: maintainers touching corresponding subsystems.
- Module docs owners: module maintainers and contributors changing those modules.

Update docs in the same PR when any of the following changes happen:
- CLI command names/flags or execution flow changes
- benchmark format/schema or validation behavior changes
- architecture-level behavior changes
- required environment variables or service dependencies change

## Diverse Reviewer Lanes

For major doc changes, request review across these roles:
- Onboarding reviewer: verifies quick-start clarity for first-time users.
- Runtime/ops reviewer: validates setup and workflow correctness.
- Module maintainer reviewer: checks module docs against implementation reality.
- Architecture reviewer: checks intent, decisions, and tradeoffs.
- Security/compliance reviewer: checks data-handling and secret-management guidance.

These reviewer lanes are role-based and can be fulfilled by any qualified maintainer/contributor.
