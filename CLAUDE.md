# Instructions
This repository CRSBench is a benchmark to evaluate CRS (Cyber Reasoning System).
Benchmark is consists of a set of projects.

## Proposed RFC
The proposed standard for CRS benchmark is in `docs/benchmark-spec.md`.

## Glossary
- CRS: cyber reasoning system
- LLM: large language model
- POV: proof of vulnerability
- CP: challenge project

## Documentation Structure
This repository has two types of documentation:

### docs/ - RFC and High-Level Usage
- Contains RFC specifications (benchmark-spec.md)
- High-level format examples (meta-example.yaml)
- User-facing documentation
- Target audience: Users and CRS developers using the benchmark

### design-docs/ - Implementation Details
- Architecture and design decisions
- Module implementation documentation
- Internal APIs and data flows
- Target audience: CRSBench contributors and curious readers

- When writing documentation, avoid lengthy or unnecessary details.
- Include only the essential features that must be implemented.
- Do not add extra requirements unless explicitly requested by the user, as doing so increases the framework’s complexity without real benefit.
- When implementing or modifying CRSBench components, refer to design-docs/ for detailed implementation guidance.

## Projects
- Each project is a directory under `benchmarks`.
- The project is Google OSS-Fuzz compatible.
- In each project, `project.yaml` is used to specify a project.
  - which programming language (e.g, C/C++, Java, Go, Rust)
  - which fuzzing engines (e.g., libfuzzer, AFL)
  - which sanitizers (e.g, ASAN, MSAN, UBSAN, Jazzer sanitizer)

## CRS Benchmark
There is an `.aixcc` directory under each project, which is used to store the
metadata of the benchmark and ground truth for each vulnerability.

## Comparison against FuzzBench
Unlike FuzzBench used to evaluate fuzzer, which only reports the
coverage/crashes.

CRSBench also stores the ground truth to catch whether a bug (POV) is actually
found or missed.

On top of that, we also provide basic infrastructure like LiteLLM to support the
need of LLM used in modern AI-powered CRS.

### CRS Execution Architecture vs FuzzBench

**Key Difference: CRS Interface Design**

CRSBench uses a fundamentally different execution model compared to FuzzBench:

- **FuzzBench Approach**: Each fuzzer has its own `fuzzer.py` with `build()` and `fuzz()` functions
  - Example: `fuzzers/aflplusplus/fuzzer.py`, `fuzzers/libfuzzer/fuzzer.py`, etc.
  - Each fuzzer directory contains fuzzer-specific build and execution logic
  - Reference: See `claude_reference_projects/fuzzbench/fuzzers/*/fuzzer.py`

- **CRSBench Approach**: Uses OSS-Fuzz's standardized interface
  - **Current Format** (using `infra/helper.py`):
    - Build CRS: `python3 infra/helper.py build_crs <crs-config-dir> <project-name>`
    - Run CRS: `python3 infra/helper.py run_crs <crs-config-dir> <project-name> <harness-name>`
  - **Future Format** (installable commands - in development by OSS-Fuzz team):
    - **`oss-fuzz-crs`**: For bug finding / vulnerability discovery CRS
      - Example: `oss-fuzz-crs build <crs-config-dir> <project-name>`
      - Example: `oss-fuzz-crs run <crs-config-dir> <project-name> <harness-name>`
    - **`oss-patch-crs`**: For patch generation / program repair CRS
      - Example: `oss-patch-crs build <crs-config-dir> <project-name>`
      - Example: `oss-patch-crs run <crs-config-dir> <project-name> <harness-name> [<pov-name>]`
      - Note: `<pov-name>` is optional - CRS can generate patches without a specific POV
    - Both will be installable via pip/uv for easier deployment
  - **No individual `fuzzer.py` per CRS** - all CRS implementations use the same interface
  - May have thin wrappers around the helper interface for convenience, but no per-CRS build/run scripts
  - This standardization simplifies CRS integration and ensures consistency

**Why This Matters**:
- CRSBench doesn't need fuzzer-specific integration code for each CRS
- All CRS implementations must conform to the OSS-Fuzz interface standard
- Build and execution logic is centralized in OSS-Fuzz infrastructure
- Easier to add new CRS implementations without modifying CRSBench core
- Supports both vulnerability discovery (`oss-fuzz-crs`) and patch generation (`oss-patch-crs`) workflows

**Implementation Considerations**:
- **Current State**: CRSBench implementation uses `python3 infra/helper.py` format
- **Future Migration**: When OSS-Fuzz team releases the installable command wrappers:
  - CRSBench should support both `oss-fuzz-crs` (bug finding) and `oss-patch-crs` (patch generation)
  - Consider feature detection: check if commands exist, fallback to `python3 infra/helper.py`
  - This ensures compatibility during the transition period
- **Design Principle**: Keep CRS execution logic flexible to accommodate command format changes

**Reference Documentation**: `docs/ossfuzz-crs-interface.md`


## Standardized CRS interface
The document for standardized CRS interface is in `docs/ossfuzz-crs-interface.md`.

CRSBench will utilize this interface to build CRS docker images and run those
docker images to evaluate CRS.

## Reference projects
Some reference projects are put in `claude_refernece_projects`, you should look
when asked.

### Docs
Please summarize reference projects by creating markdown files in `docs_reference_projects`.

## Coding standard

### Module Organization
- **Only `run_experiment.py` should be at the root of `crsbench/` directory**
  - This is the main CLI entry point for the `crsbench` command
  - All other functionality should be organized into appropriate subdirectories
- Create dedicated modules for major features (e.g., `crsbench/distributed/`, `crsbench/validation/`)
- Keep modules focused and cohesive
- Avoid creating files in the root `crsbench/` directory unless they are:
  - Entry points (like `run_experiment.py`)
  - Package initialization (`__init__.py`)

### Python
- use absolute import instead of relative import; so moving files around for
  restructuring is straightforward without further editing import statements.

### Testing
- Follow TDD (Test-Driven Development) design when applicable
- **MUST run corresponding tests when modifying a module**
- Test files are located in `tests/` directory
- Test file naming: `test_<module_name>.py` (e.g., `test_validation.py`)
- Run tests with: `pytest tests/test_<module_name>.py -v`
- Run with coverage: `pytest tests/test_<module_name>.py --cov=crsbench.<module_name>`
- Update tests when changing module behavior or adding features
- **DO NOT use `cat` command or heredocs to create test files**
  - Create proper Python test scripts using the Write tool
  - Use Python's `tempfile` module for temporary test data
  - Use `with open()` context managers for file operations in tests
  - Example: See `test_orchestrator_e2e.py` for proper test file creation

## Documents standard
- create entry in README.md when adding a new component.
- create a README.md to summarize each component in their own directories.

## Design Documentation Requirement
- **MUST create design document in `design-docs/` before implementing new features**
- Design doc should cover:
  - Architecture overview and component interaction
  - Data flow and API design
  - File structure and module organization
  - Integration points with existing code
  - Testing strategy
- Reference: See `design-docs/` directory for examples
- Target audience: Implementation developers and code reviewers

### Design Docs Organization
Design documents are organized by CRSBench module:

```
design-docs/
├── validation/              # Validation module design docs
│   └── validation.md
├── evaluation/              # Evaluation module design docs
│   ├── evaluation.md
│   └── crs-executors.md
├── migration/               # Migration module design docs
│   └── migration-atlanta-to-rfc.md
├── distributed/             # Distributed execution design docs
│   └── distributed-job-queue.md
└── architecture.md          # General architecture (root level)
```

**Rules**:
- Module-specific docs go in `design-docs/<module-name>/`
- Only create directories for actual `crsbench/` modules (validation, evaluation, migration, distributed, etc.)
- General/cross-cutting docs (like architecture.md) stay at `design-docs/` root
- Do NOT create directories for non-existent modules (e.g., general/, orchestrator/)
- Match directory names exactly to `crsbench/` module names

## Usage or reference of third party codebase
- Please document the usage of third party codebase for good open-source gesture
  :)

## Testing crsbench CLI
The main CLI entry point is `crsbench` command provided by `crsbench/run_experiment.py`.

### Installation
Install the package in editable mode:
```bash
uv pip install -e .
```

This creates the `crsbench` executable in `.venv/bin/crsbench`.

### Running crsbench
After installation, run it using:
```bash
# From within venv
.venv/bin/crsbench --help

# Or as a Python module
python -m crsbench.run_experiment --help

# Example with arguments
.venv/bin/crsbench \
  --experiment-config config.yaml \
  --benchmarks bench1,bench2 \
  --experiment-name test-exp \
  --crses atlantis-c,atlantis-multilang
```

### Testing argument parsing
```bash
# Create test config
echo "trials: 1" > /tmp/test-config.yaml

# Test with sample arguments
python crsbench/run_experiment.py \
  --experiment-config /tmp/test-config.yaml \
  --benchmarks bench1,bench2 \
  --experiment-name test \
  --crses crs1,crs2
```

## Other instructions
- read TODO.md in the new session.
- use uv as python package manager
- don't consider backward compatability (no cpvs; just throw errors for old formats)
