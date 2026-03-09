# Design Doc Authoring Guidelines

This guide defines how to write and maintain `docs/design/` content in CRSBench.

## Intent

- Keep design docs stable and contract-focused.
- Avoid implementation-copy drift.
- Make distributed behavior explicit and testable.

## Content Rules

### MUST

- State `Audience` and `Scope` near the top.
- Describe contracts, not internal coding steps:
  - invariants
  - input/output semantics
  - state transitions
  - failure/retry behavior
  - compatibility constraints (local + distributed)
- Keep one canonical section per contract topic, and cross-link from other docs.
- Choose canonical ownership using `docs/design/README.md` ("Canonical Contract Map"); update that map when introducing a new contract topic.
- Treat `architecture.md` as overview/context unless the map explicitly declares it canonical for a specific contract topic.
- Include deployment/runtime behavior for non-local paths when relevant.
- Update the corresponding design doc whenever behavior changes.

### SHOULD

- Follow a consistent architecture structure inspired by arc42/C4:
  - goals/non-goals
  - constraints
  - context and boundaries
  - solution strategy
  - runtime/deployment behavior
  - decisions, risks, and validation
- Use short pseudocode only when it clarifies behavior.
- Keep implementation pointers brief (few file links, no line-number coupling).

### AVOID

- Large copied code blocks from current implementation.
- Implementation checklists (`- [ ]`, `- [x]`) and roadmap tracking in design docs.
- Commit/date-pinned implementation narratives.
- "Historical/outdated" disclaimers in normative sections.
- Duplicate contract definitions across multiple files.

## Minimal Template

```md
# <Title>
- Audience: <who>
- Scope: <boundary>
- Related: <links>

## Goals and Non-goals
## Constraints
## Context and Boundaries
## Contract (invariants, interfaces, semantics)
## Runtime Behavior (happy path + failure/retry)
## Deployment/Distributed Behavior
## Decisions and Tradeoffs
## Risks and Validation
## Implementation Pointers (short links only)
```

## Practical Review Checklist

Before finalizing a design-doc change, verify:

1. Is this still correct if implementation details get refactored?
2. Are contracts defined in one canonical place?
3. Are failure/retry and distributed semantics explicit?
4. Did we avoid implementation snapshots and checklist drift?
5. If behavior changed, did we update this doc in the same change?

## References

- Diataxis: <https://diataxis.fr/>
- arc42: <https://arc42.org/overview>
- C4 model: <https://c4model.com/diagrams>
- Docs as Code: <https://www.writethedocs.org/guide/docs-as-code/>
