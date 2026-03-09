# Documentation Inventory and Audit

Last updated: 2026-02-21

This file records current documentation scope, canonical placement, and cleanup findings.

## Scope

### In-scope canonical project docs
- Root: `README.md`, `CONTRIBUTING.md`
- Primary docs tree: all files under `docs/`
- Adjacent local component READMEs:
  - `scripts/README.md`
  - `benchmark-suites/README.md`
  - `experiment-configs/README.md`
  - `snapshot-examples/README.md`
  - `dashboard/README.md`
- In-repo integrated upstream docs:
  - `oss-crs/README.md`
  - `third_party/oss-fuzz/README.md`

### Out-of-scope for canonical consolidation
- Tool-generated docs (for example `.pytest_cache/README.md`)
- Deep upstream vendor docs beneath `third_party/oss-fuzz/docs/**` unless explicitly linked as local references

## Inventory Summary

- `docs/` markdown and yaml documents: 58
- Root canonical docs: 2
- Adjacent local component README files: 9
- In-repo integrated upstream entry docs: 2

## Classification

| Category | Canonical roots |
|---|---|
| Overview | `README.md`, `docs/README.md` |
| Setup and operations | `docs/environment-setup.md`, `docs/experiment-workflow.md`, `docs/testing-setup.md` |
| Contributor workflows | `CONTRIBUTING.md`, `docs/framework-developer-guide.md`, `docs/benchmark-developer-guide.md` |
| Architecture and rationale | `docs/design/README.md`, `docs/design/architecture.md`, `docs/design/**` |
| Module reference | `docs/modules/README.md`, `docs/modules/**` |
| Specifications/contracts | `docs/RFC.md`, config examples under `docs/*.yaml` |

## Findings: Staleness, Contradictions, Overlap

### Confirmed stale or inconsistent items
- `docs/design/architecture.md` previously referenced legacy incremental-build internals and older install flow details not aligned with current repository and root quick-start.

### Overlap candidates
- Root `README.md` and `docs/experiment-workflow.md` both describe runtime commands.
  - Resolution: keep root as summary entry; keep detailed guidance in `docs/experiment-workflow.md`.
- Root `README.md` and `docs/environment-setup.md` both describe service/env setup.
  - Resolution: keep root minimal; keep env variable and scenario detail in `docs/environment-setup.md`.
- `docs/benchmark-spec.md` and `docs/RFC.md` overlap by topic.
  - Resolution: retain `docs/benchmark-spec.md` as compatibility pointer only.

### Contradictions to watch (review checklist)
- Installation workflow wording (`uv sync` versus alternative local install patterns).
- Local-only vs distributed runtime prerequisites.
- Service naming consistency (`Valkey/Redis`) and queue terminology.

## Canonical Decisions Applied in This Change

1. Root README remains summary-only for setup and runtime links.
2. `docs/README.md` is the canonical navigation hub.
3. Design rationale is anchored in `docs/design/architecture.md`.
4. Pointer-page pattern is retained for benchmark spec legacy links.
5. Documentation governance moved into explicit docs:
   - `docs/documentation-taxonomy.md`
   - `docs/documentation-maintenance.md`

## Remaining Follow-up Candidates

- Sweep adjacent component READMEs to ensure they point to canonical docs and avoid duplicating global setup instructions.
- Optional automated lint/check for local markdown links in CI.
