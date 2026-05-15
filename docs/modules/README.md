# Module Documentation

Module-specific documentation moved from `crsbench/*/README.md` into this folder.
This subtree is for module-scoped reference and short operational notes only.
Primary onboarding and cross-cutting workflows belong under `docs/getting-started/`,
`docs/experiments/`, `docs/deployment/`, or `docs/operations/`.

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
  - linked outward to canonical guides or reference pages where needed
- Keep deep architecture/mechanism details out of module pages unless they are
  necessary to understand the module's public contract.
- Put first-time-user and cross-module workflows in `docs/getting-started/`, `docs/experiments/`, `docs/deployment/`, or `docs/operations/`.
- Do not use module pages for onboarding, cross-module workflows, primary CLI tutorials, or implementation tracking.
