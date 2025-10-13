# Instructions
This repository is a benchmark to evaluate CRS (Cyber Reasoning System).
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

When implementing or modifying CRSBench components, refer to design-docs/ for detailed implementation guidance.

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

## Old format used for internal usage
The current project use old format for internal testing when developing CRS.
We would like standardize it by enhancing it.

Example project is in `benchmarks-internal/r3_5-binutils`.

## Official AIxCC Benchmark for CRS.
AIxCC organizer also design a benchmark used to evaluate each team's CRS.
We would like to build on top of that and improve them.
Therefore, the official benchmark is provided in directory `benchmark-afc`.
The goal is to find a superset of features between ours and official one to
define a new standard for CRS benchmark and migrate them to the new standard.

Example project is in `benchmarks-afc/official-afc-systemd`.

## Comparison against FuzzBench
Unlike FuzzBench used to evaluate fuzzer, which only reports the
coverage/crashes.

CRSBench also stores the ground truth to catch whether a bug (POV) is actually
found or missed.

On top of that, we also provide basic infrastructure like LiteLLM to support the
need of LLM used in modern AI-powered CRS.


## Some variable in meta.yaml
- "$PROJECT": the project directory
- "$REPO": the source code repository directory

"$PROJECT" and "$REPO" both exists and are not interchangeable.


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
