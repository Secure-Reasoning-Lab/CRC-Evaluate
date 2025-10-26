# Reference Projects Documentation

This directory contains documentation for reference projects that inform the design and implementation of CRSBench.

## Directory Structure

```
docs_reference_projects/
├── fuzzbench/                 # FuzzBench - fuzzer evaluation platform
├── patchagent/               # PatchAgent - automated patch generation
├── crs-multilang-e2e-eval/   # CRS multilang end-to-end evaluation
└── scoring-pipeline/         # AIXCC scoring pipeline
```

## Reference Projects

### FuzzBench
Google's fuzzer evaluation platform. CRSBench draws inspiration from FuzzBench's:
- Template-based Docker build system
- Multi-stage build pipeline
- Configuration-driven approach
- Snapshot and trial management

**Documentation:**
- [FUZZBENCH-INDEX.md](fuzzbench/FUZZBENCH-INDEX.md) - Navigation guide and quick lookup
- [FUZZBENCH-OVERVIEW.md](fuzzbench/FUZZBENCH-OVERVIEW.md) - Master overview and architecture
- [fuzzbench-build-architecture.md](fuzzbench/fuzzbench-build-architecture.md) - Build pipeline details
- [fuzzbench-docker-build-process.md](fuzzbench/fuzzbench-docker-build-process.md) - Docker build process
- [fuzzbench-redis-architecture.md](fuzzbench/fuzzbench-redis-architecture.md) - Redis infrastructure
- [fuzzbench-snapshots.md](fuzzbench/fuzzbench-snapshots.md) - Snapshot system
- [README-fuzzbench.md](fuzzbench/README-fuzzbench.md) - Quick reference

### PatchAgent
Automated patch generation system.

**Documentation:**
- [patchagent.md](patchagent/patchagent.md)

### CRS Multilang E2E Eval
End-to-end evaluation framework for multilanguage CRS systems.

**Documentation:**
- [crs-multilang-e2e-eval.md](crs-multilang-e2e-eval/crs-multilang-e2e-eval.md)

### Scoring Pipeline
AIXCC scoring and deduplication pipeline.

**Documentation:**
- [aixcc-scoring-pipeline-deduplication.md](scoring-pipeline/aixcc-scoring-pipeline-deduplication.md)

## Adding New Reference Documentation

When documenting a new reference project:

1. Create a subdirectory: `docs_reference_projects/<project-name>/`
2. Match the subdirectory name to the reference project in `claude_reference_projects/`
3. Add documentation files to the subdirectory
4. For multi-document projects, include a README or index file
5. Update this README with a link to the new documentation
6. Update `CLAUDE.md` if the reference project has special significance
