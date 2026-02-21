# Contributing to CRSBench

Thank you for your interest in contributing to CRSBench.

For contribution tracks and development workflows, start with:

- Framework development: [docs/framework-developer-guide.md](docs/framework-developer-guide.md)
- Benchmark development: [docs/benchmark-developer-guide.md](docs/benchmark-developer-guide.md)
- Experiment/runtime usage: [docs/experiment-workflow.md](docs/experiment-workflow.md)
- Documentation index: [docs/README.md](docs/README.md)

## Governance

### Technical Steering Committee (TSC)

The Technical Steering Committee (TSC) is responsible for technical oversight of CRSBench.
TSC voting members are the project's Maintainers. Decisions are made by consensus when possible; when a vote is needed, each voting member has one vote and a majority of those present (with quorum) is required.

### Roles

**Contributors** are anyone in the technical community who contributes code, documentation, benchmarks, or other technical artifacts.

**Maintainers** are Contributors who can approve and merge changes to project repositories. A Contributor may become a Maintainer by majority approval of the TSC. Maintainers serve as TSC voting members.

### Initial Maintainers (alphabetic order)

| Name             | Organization                                | GitHub     |
|------------------|---------------------------------------------|------------|
| Andrew Chin      | Georgia Institute of Technology             | `@azchin`  |
| Cen Zhang        | Georgia Institute of Technology             | `@occia`   |
| Dongkwan Kim     | Georgia Institute of Technology             | `@0xdkay`  |
| Fabian Fleischer | Georgia Institute of Technology             | `@fab1ano` |
| Jiho Kim         | Georgia Institute of Technology             | `@jhkimx2` |
| Taesoo Kim       | Georgia Institute of Technology & Microsoft | `@tsgates` |
| Younggi Park     | Independent Researcher                      | `@grill66` |
| Youngjoon Kim    | Georgia Institute of Technology             | `@acorn421` |
| Yu-Fu Fu         | Georgia Institute of Technology             | `@fuyu0425` |

## Reporting Issues

Use the GitHub issue tracker for bugs and tasks. Include:

- Observed and expected behavior
- Reproduction steps (exact commands/configs)
- Environment details (OS, Docker, Redis/Valkey, relevant paths)

## Contributing Code

If you have a fix or feature:

1. Branch from `main`
2. Implement and test locally
3. Rebase on latest `main` before opening PR
4. Open a PR and request review

For commit messages, use Conventional Commits where possible:
- `fix:`
- `feat:`
- `chore:`
- `docs:`
- `refactor:`

## Documentation Policy

- User-facing docs: `docs/`
- Design/implementation docs: `docs/design/`
- Module docs: `docs/modules/`
- Keep docs updated in the same PR when behavior changes.

## Repository Scope

This repository includes CRSBench and related in-tree components (including `oss-crs/`).
Contribution policy is defined by this repository.
