# Documentation Maintenance Guide

This guide defines how CRSBench documentation stays clean, current, and non-redundant.

## Core Rules

1. One topic, one canonical page.
2. Summary pages link to deep pages instead of duplicating them.
3. Architecture intent and tradeoffs belong in `docs/design/`.
4. Behavior changes require doc updates in the same PR.

## Change Checklist

When editing docs, ensure:
- the canonical page is updated first
- duplicate text is removed or converted to pointer links
- navigation links in `README.md` and `docs/README.md` still route correctly
- commands and config snippets match current repository behavior

## Reviewer Matrix (Diverse Expert Lanes)

For substantial documentation changes, assign at least three reviewer lanes:
- Onboarding lane: first-time user clarity and quick-start accuracy.
- Runtime/ops lane: deployment and execution correctness.
- Architecture lane: design intent, constraints, and tradeoff quality.
- Module maintainer lane: module-specific operational accuracy.
- Security/compliance lane: handling of secrets, data, and risk statements.

One person may cover multiple lanes if qualified, but lane coverage must be explicit in review notes.

## Ownership and Escalation

- Maintainers own `README.md`, `docs/README.md`, and this guide.
- Subsystem maintainers own relevant `docs/design/**` and `docs/modules/**`.
- If documentation and implementation disagree, prioritize implementation truth and update docs before merge.

## Recommended Review Batches

For large doc migrations, split into:
1. structure and navigation
2. content refresh and de-duplication
3. intent/rationale updates
4. cleanup and validation
