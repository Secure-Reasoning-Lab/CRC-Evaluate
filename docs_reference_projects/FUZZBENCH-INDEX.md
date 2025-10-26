# FuzzBench Documentation Index

Complete documentation of FuzzBench's Docker image build process.

## Start Here

**New to FuzzBench?** Read in this order:
1. FUZZBENCH-OVERVIEW.md (this explains the big picture)
2. README-fuzzbench.md (key concepts)
3. fuzzbench-build-architecture.md (visual understanding)
4. fuzzbench-docker-build-process.md (detailed technical)

## Document Map

### FUZZBENCH-OVERVIEW.md
**What**: Master overview and navigation guide
**Why**: Explains the complete system architecture and design decisions
**How long**: 10-15 minutes
**Best for**: Understanding the entire system, architecture decisions, applying to CRSBench

Key sections:
- The Build System at a Glance
- Core Concepts Explained
- The Build Pipeline (6 levels)
- Key Design Decisions
- File Organization
- How Each Part Works Together
- Application to CRSBench

### README-fuzzbench.md
**What**: Quick reference guide
**Why**: Summarizes key concepts and provides quick lookup
**How long**: 5-10 minutes
**Best for**: Quick answers, definitions, quick reference

Key sections:
- Contents (what each document covers)
- Quick Reference: The Build Flow
- Key Concepts
- Build Process in 6 Steps
- Multi-Stage Build Advantage
- How to Read the Docs
- Applying This to CRSBench

### fuzzbench-docker-build-process.md
**What**: Complete technical deep dive
**Why**: Explains every file, every dockerfile, complete process
**How long**: 20-30 minutes
**Best for**: Implementation details, understanding code, troubleshooting

Key sections:
- Directory Structure (complete with annotations)
- Key Files (8 critical files explained)
- Dockerfiles (5 different dockerfile types with code)
- Build Process Step-by-Step (6 detailed steps)
- Makefile Generation
- Key Design Patterns
- Environment Variables
- Summary

### fuzzbench-build-architecture.md
**What**: Visual architecture guide with ASCII diagrams
**Why**: Shows how pieces fit together visually
**How long**: 15-20 minutes
**Best for**: Visual learners, understanding dependencies, Makefile rules

Key sections:
- High-Level Architecture Diagram
- Dependency Chain (6 levels with ASCII art)
- File Organization (visual tree)
- Data Flow Through the Build Process
- Build Stages in Detail
- Makefile Rule Example
- Key Insights

## Quick Lookup

### I want to understand...

**The complete system:**
→ Read FUZZBENCH-OVERVIEW.md (the big picture)

**How docker images are built:**
→ Read fuzzbench-build-architecture.md (visual flow) then fuzzbench-docker-build-process.md (details)

**What each file does:**
→ See "Key Files" section in fuzzbench-docker-build-process.md

**How benchmarks work:**
→ See "Directory Structure" → "benchmarks/" in fuzzbench-docker-build-process.md

**How fuzzers integrate:**
→ See "Directory Structure" → "fuzzers/" in fuzzbench-docker-build-process.md

**Why certain design decisions were made:**
→ Read "Key Design Decisions" in FUZZBENCH-OVERVIEW.md

**The dependency chain:**
→ See "Dependency Chain" in fuzzbench-build-architecture.md

**What Dockerfiles do:**
→ See "Key Files" → "4. Dockerfiles" in fuzzbench-docker-build-process.md

**How to add new benchmarks/fuzzers:**
→ See "Applicability to CRSBench" sections in multiple docs

**The complete build process:**
→ Read "Build Process Step-by-Step" in fuzzbench-docker-build-process.md

**Environment variables:**
→ See "Environment Variables Used" in fuzzbench-docker-build-process.md or "Environment Variables" table in FUZZBENCH-OVERVIEW.md

## Time-Based Reading Guide

### 5 minutes
1. This page (orientation)
2. "Quick Reference: The Build Flow" in README-fuzzbench.md
3. Skip to questions section at end of FUZZBENCH-OVERVIEW.md

### 15 minutes
1. FUZZBENCH-OVERVIEW.md (complete)
2. "Core Concepts Explained" in FUZZBENCH-OVERVIEW.md
3. "The Build Pipeline" in FUZZBENCH-OVERVIEW.md

### 30 minutes
1. FUZZBENCH-OVERVIEW.md (complete)
2. README-fuzzbench.md (complete)
3. Diagrams in fuzzbench-build-architecture.md

### 60 minutes
1. FUZZBENCH-OVERVIEW.md (complete)
2. README-fuzzbench.md (complete)
3. fuzzbench-build-architecture.md (complete)
4. fuzzbench-docker-build-process.md ("Overview" section)

### Deep dive (2+ hours)
1. Read all documents in order
2. Study code snippets
3. Answer test questions at end of FUZZBENCH-OVERVIEW.md
4. Compare with actual files in reference repository

## Search Quick Links

**Looking for information about...**

- **image_types.yaml**: See "Key Files" section 1 in fuzzbench-docker-build-process.md
- **docker_images.py**: See "Key Files" section 2 in fuzzbench-docker-build-process.md
- **generate_makefile.py**: See "Key Files" section 3 in fuzzbench-docker-build-process.md
- **Benchmark Dockerfile**: See "Key Files" section 4 in fuzzbench-docker-build-process.md
- **Fuzzer builder.Dockerfile**: See "Key Files" section 4 in fuzzbench-docker-build-process.md
- **benchmark-builder/Dockerfile**: See "Key Files" section 4 in fuzzbench-docker-build-process.md
- **benchmark-runner/Dockerfile**: See "Key Files" section 4 in fuzzbench-docker-build-process.md
- **checkout_commit.py**: See "Build Process Step-by-Step" → Step 3 in fuzzbench-docker-build-process.md
- **fuzzer_build script**: See "Build Process Step-by-Step" → Step 3 in fuzzbench-docker-build-process.md

## Key Concepts Explained

All documents provide detailed explanations of:
- Templates and instantiation
- Multi-stage Docker builds
- Dynamic Python integration
- Metadata-driven reproducibility
- Configuration over code
- Separation of concerns
- Environment variables
- Build orchestration

See specific documents for details:
- Concepts overview: FUZZBENCH-OVERVIEW.md
- Quick definitions: README-fuzzbench.md
- Detailed explanations: fuzzbench-docker-build-process.md
- Visual explanations: fuzzbench-build-architecture.md

## Files Analyzed

All documents reference these files in the FuzzBench repository:

**Core Infrastructure:**
- docker/image_types.yaml
- docker/generate_makefile.py
- experiment/build/docker_images.py
- docker/base-image/Dockerfile
- docker/benchmark-builder/Dockerfile
- docker/benchmark-runner/Dockerfile
- docker/benchmark-builder/checkout_commit.py
- docker/benchmark-builder/fuzzer_build

**Example Files:**
- benchmarks/libpng_libpng_read_fuzzer/Dockerfile
- benchmarks/libpng_libpng_read_fuzzer/benchmark.yaml
- fuzzers/libfuzzer/fuzzer.py
- fuzzers/libfuzzer/builder.Dockerfile
- fuzzers/libfuzzer/runner.Dockerfile

Reference repository: `/Users/fuyu0425/aixcc/CRSBench/claude_reference_projects/fuzzbench/`

## Document Statistics

| Document | Lines | Focus | Audience |
|----------|-------|-------|----------|
| FUZZBENCH-OVERVIEW.md | 396 | Architecture, design decisions | Everyone |
| README-fuzzbench.md | 168 | Concepts, quick reference | Quick learners |
| fuzzbench-docker-build-process.md | 608 | Technical details, code | Developers |
| fuzzbench-build-architecture.md | 422 | Visual diagrams, flow | Visual learners |
| **Total** | **1,594** | **Complete coverage** | **All audiences** |

## Diagrams Included

- System-level architecture (4 versions)
- 6-level dependency chain
- File organization tree
- Data flow diagrams
- Build stage breakdowns
- Makefile rule examples

## Code Snippets Included

- 25+ code examples
- Dockerfile excerpts
- Python code samples
- Makefile rules
- Shell scripts
- YAML configuration examples

## Best Practices Identified

1. Template-based instantiation
2. Multi-stage Docker builds
3. Dynamic language integration
4. Metadata-driven design
5. Configuration over code
6. Modular responsibility
7. Dependency tracking
8. Reproducibility through versioning

## Application to CRSBench

All documents include guidance on applying FuzzBench's approaches to CRSBench:
- YAML template systems for CRS types
- Dynamic integration with crs.build() and crs.run()
- Separating builder and runner responsibilities
- Metadata storage for reproducibility
- Auto-generating build infrastructure

## Questions to Test Understanding

Test your understanding with the questions at the end of FUZZBENCH-OVERVIEW.md:
1. Why does FuzzBench use templates?
2. How does dynamic import reduce code?
3. What are the 6 build levels?
4. Why separate builder/runner?
5. How is reproducibility maintained?
6. What files does a benchmark need?
7. What files does a fuzzer need?
8. How does generate_makefile.py work?
9. Where is build() called?
10. How is the commit hash used?

## Getting Started

1. **Start here**: This page (orientation)
2. **Read next**: FUZZBENCH-OVERVIEW.md
3. **For concepts**: README-fuzzbench.md
4. **For visuals**: fuzzbench-build-architecture.md
5. **For details**: fuzzbench-docker-build-process.md

## Navigation Tips

- Use document table of contents to jump to sections
- Use "Quick Lookup" section above to find specific information
- Each document references the others for detailed information
- Diagrams appear in fuzzbench-build-architecture.md
- Code snippets appear in fuzzbench-docker-build-process.md

## Related Documentation

- CRSBench Architecture: `/docs/architecture.md` or `design-docs/architecture.md`
- CRS Interface: `/docs/ossfuzz-crs-interface.md`
- Benchmark Specification: `/docs/benchmark-spec.md`
- FuzzBench Repository: `/claude_reference_projects/fuzzbench/`

---

**Last Updated**: October 26, 2024
**Coverage**: Complete Docker build system
**Depth**: Comprehensive (overview to deep technical)
**Audience**: All skill levels
