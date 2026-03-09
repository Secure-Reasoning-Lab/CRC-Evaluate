# Design: Atlanta-to-RFC Migration
- Audience: maintainers migrating historical Team-Atlanta benchmark material into CRSBench benchmark format
- Scope: migration contracts, artifact mapping, and validation semantics
- Related: [RFC](../../RFC.md), [Migration Validation](./migration-validation.md)

## Goals and Non-goals

### Goals
- define how source artifacts map into CRSBench benchmark structure
- preserve benchmark semantics during migration
- make migration outputs validate cleanly against the CRSBench benchmark contract

### Non-goals
- step-by-step migration tutorials
- implementation snapshots of converter scripts
- historical process notes tied to one migration run

## Source and Target Contract

### Source
Migration consumes Team-Atlanta style benchmark/project material, including source artifacts, patch/POV/crash-log material, and benchmark configuration.

### Target
Migration produces CRSBench-compatible benchmark artifacts with:
- benchmark root files
- `.aixcc` hierarchy in RFC/CRSBench layout
- converted metadata required by validation and runtime tooling

## Mapping Invariants

- semantic identity of each CPV must be preserved
- patch, POV, and crash-log material must remain associated with the same harness/CPV after migration
- migrated output must be sufficient for benchmark validation and runtime execution

## Migration Phases

1. load and inspect source project
2. transform root configuration into CRSBench benchmark metadata
3. rewrite artifact locations into CRSBench `.aixcc` layout
4. normalize filenames and IDs to CRSBench conventions
5. validate migrated output and surface issues

## Failure Semantics

- unreadable source input is a migration precondition failure
- unmappable or ambiguous artifact structure must be reported explicitly
- partial migration output must not be presented as valid without validation status

## Decisions and Tradeoffs

- decision: use explicit mapping rules rather than heuristic best-effort conversion
  - tradeoff: more migration failures up front, safer resulting benchmarks
- decision: keep validation as a first-class post-migration phase
  - tradeoff: additional work, stronger confidence in migrated output

## Validation

This contract should be covered by:
- migration mapping tests
- artifact-preservation tests
- migration validation tests against known source/target pairs

## Implementation Pointers

- migration conversion modules under `crsbench/migration/`
- `tests/` migration-related suites
