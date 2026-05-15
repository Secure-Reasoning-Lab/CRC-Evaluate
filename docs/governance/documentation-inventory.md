# Documentation Inventory and Audit

Last updated: 2026-03-09

This file records documentation scope and cleanup findings. It is an audit
artifact, not a policy source of truth.

## Scope

### In-scope canonical project docs
- Root: `README.md`, `CONTRIBUTING.md`
- Primary docs tree: all files under `docs/`
- Adjacent local component READMEs:
  - `scripts/README.md`
  - `benchmark-suites/README.md`
  - `experiment-configs/README.md`
  - `dashboard/README.md`
- In-repo integrated upstream docs:
  - `oss-crs/README.md`
  - `third_party/oss-fuzz/README.md`

### Out-of-scope for canonical consolidation
- Tool-generated docs (for example `.pytest_cache/README.md`)
- Deep upstream vendor docs beneath `third_party/oss-fuzz/docs/**` unless explicitly linked as local references

## Inventory Summary

- `docs/` markdown and yaml documents: 88
- Root canonical docs: 2
- Adjacent local component README files: 8
- In-repo integrated upstream entry docs: 2

## Classification

| Category | Canonical roots |
|---|---|
| Overview | `README.md`, `docs/README.md` |
| Setup and operations | `docs/getting-started/**`, `docs/guides/**` |
| Contributor workflows | `CONTRIBUTING.md`, `docs/contributors/**` |
| Architecture and rationale | `docs/design/README.md`, `docs/design/architecture.md`, `docs/design/**` |
| Module reference | `docs/modules/README.md`, `docs/modules/**` |
| Specifications/contracts | `docs/RFC.md`, config examples under `docs/*.yaml` |

## Findings: Staleness, Contradictions, Overlap

### Confirmed stale or inconsistent items
- `docs/design/architecture.md` previously referenced legacy incremental-build internals and older install flow details not aligned with current repository and root quick-start.

### Overlap candidates
- Root `README.md` and `docs/guides/**` both describe runtime commands.
  - Resolution: keep root as summary entry; keep detailed guidance in `docs/guides/**`.
- Root `README.md` and `docs/getting-started/**` both describe service/env setup.
  - Resolution: keep root minimal; keep env variable and scenario detail in `docs/getting-started/**`.
- `docs/RFC.md` is the single canonical benchmark RFC/specification page.
  - Resolution: keep only `docs/RFC.md` as the canonical owner.

### Contradictions to watch (review checklist)
- Installation workflow wording (`uv sync` versus alternative local install patterns).
- Local-only vs distributed runtime prerequisites.
- Service naming consistency (`Valkey/Redis`) and queue terminology.

## Canonical Decisions Applied in This Change

1. Root README remains summary-only for setup and runtime links.
2. `docs/README.md` is the canonical navigation hub.
3. Design rationale is anchored in `docs/design/README.md`, with `docs/design/architecture.md` as a primary architecture entry within that tree.
4. Legacy pointer pages were removed after references were updated to canonical paths.
5. Documentation governance is anchored in `docs/governance/documentation-taxonomy.md`.

## Remaining Follow-up Candidates

- Sweep adjacent component READMEs to ensure they point to canonical docs and avoid duplicating global setup instructions.
- Extend docs contract coverage only when a concrete stale-doc regression appears.
- Continue reducing procedure-heavy content under `docs/design/**` when a user-facing guide/reference page is the better canonical owner.
