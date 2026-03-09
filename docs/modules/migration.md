# CRSBench Migration Module

This page is the module-level index for migration-related tooling. It points to
the canonical design docs for each subsystem instead of duplicating their
workflow documentation.

## Scope

The migration area covers:
- Team-Atlanta to CRSBench benchmark conversion
- `test.sh` generation and optional container-assisted refinement
- `vuln.yaml` generation from benchmark evidence
- shared repository acquisition/reuse utilities

Runnable maintainer workflows belong in contributor documentation. This page
records the subsystem boundaries and where to find their canonical design docs.

## Subsystems

### Atlanta-to-RFC Migration

Converts historical Team-Atlanta benchmark material into CRSBench benchmark
layout while preserving harness/CPV identity and validation compatibility.

Canonical design doc:
- [Atlanta-to-RFC migration](../design/migration/migration-atlanta-to-rfc.md)

### test.sh Generator

Generates benchmark-scoped `test.sh` artifacts and supporting notes from
repository context, optionally with iterative container-assisted validation.

Canonical design docs:
- [test.sh generator](../design/migration/test-sh-generator.md)

### vuln.yaml Generator

Derives CPV vulnerability metadata and supporting diagnostics from crash logs,
POVs, source context, and existing benchmark evidence.

Canonical design docs:
- [vuln.yaml generator](../design/migration/vuln-yaml-generator.md)

### Repository Manager

Provides shared repository acquisition, cache reuse, and revision checkout
contracts used by migration and generation tooling.

Canonical design docs:
- [repository manager](../design/migration/repo-manager.md)

### Migration Validation

Validates migrated source and target artifacts against the CRSBench benchmark
layout and issue taxonomy.

Canonical design docs:
- [migration validation](../design/migration/migration-validation.md)

## Module Boundaries

- `crsbench/migration/` contains maintainer-oriented migration and generation
  tooling
- the canonical contracts for those tools live under `docs/design/migration/`
- this page should stay a concise module index rather than a tutorial or
  multi-tool handbook
