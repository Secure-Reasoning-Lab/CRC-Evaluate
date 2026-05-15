# Module Documentation

Module-specific documentation moved from `crsbench/*/README.md` into this folder.
This subtree is for module-scoped reference and short operational notes only.
Primary onboarding and cross-cutting workflows belong under `docs/getting-started/`
or `docs/guides/`. These pages should link to detailed design docs under
`docs/design/` instead of duplicating implementation detail.

## Available Modules

- [Benchmark](./benchmark/README.md)
- [Benchmark Generation](./benchmark/generation.md)
- [Dataset](./dataset.md)
- [Hint Generation](./hint-generation.md)
- [Reporting](./reporting.md)
- [Statistics](./statistics.md)
- [Validation](./validation.md)

## Maintenance Rule

- Keep module pages:
  - scoped to one subsystem
  - short and reference-oriented
  - linked outward to canonical guides or design docs where needed
- Put deep architecture/mechanism details in `docs/design/`.
- Put first-time-user and cross-module workflows in `docs/getting-started/` or `docs/guides/`.
- Do not use module pages for onboarding, cross-module workflows, primary CLI tutorials, or implementation tracking.
