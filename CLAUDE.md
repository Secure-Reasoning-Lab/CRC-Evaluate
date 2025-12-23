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
- **MUST create design document in `design-docs/` before implementing new features**
- Architecture and design decisions
- Module implementation documentation
- Internal APIs and data flows
- Target audience: CRSBench contributors and curious readers

**Design doc should cover:**

- Architecture overview and component interaction
- Data flow and API design
- File structure and module organization
- Integration points with existing code
- Testing strategy

**Organization:**

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

**Rules:**

- Module-specific docs go in `design-docs/<module-name>/`
- Only create directories for actual `crsbench/` modules (validation, evaluation, migration, distributed, etc.)
- General/cross-cutting docs (like architecture.md) stay at `design-docs/` root
- Do NOT create directories for non-existent modules (e.g., general/, orchestrator/)
- Match directory names exactly to `crsbench/` module names

**Writing guidelines:**

- Avoid lengthy or unnecessary details
- Include only the essential features that must be implemented
- Do not add extra requirements unless explicitly requested by the user, as doing so increases the framework's complexity without real benefit
- When implementing or modifying CRSBench components, refer to design-docs/ for detailed implementation guidance

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
  - **Current Format** (installable commands):
    - **`oss-bugfind-crs`**: For bug finding / vulnerability discovery CRS
      - Build: `oss-bugfind-crs build <crs-config-dir> <project-name>`
      - Run: `oss-bugfind-crs run <crs-config-dir> <project-name> <harness-name> [--output <dir>] [--hints <hints-dir>]`
      - Config path example: `example_configs/ensemble-c` (no `infra/crs/` prefix)
    - **`oss-bugfix-crs`**: For patch generation / program repair CRS
      - Build: `oss-bugfix-crs build <crs-config-dir> <project-name> --oss-fuzz $OSS_FUZZ_HOME`
      - Run: `oss-bugfix-crs run <crs-config-dir> <project-name> --harness <harness-name> [--pov <pov-file> | --povs <povs-dir>] [--hints <hints-dir>] [--output <dir>] --litellm-base <url> --litellm-key <key>`
      - Note: `--pov` specifies a single POV file (mounted to `/pov` in container)
      - Note: `--povs` specifies a directory containing POVs (mounted to `/povs/` in container)
      - Note: If neither specified, CRS processes all available POVs (implementation-dependent)
      - Note: `--hints` provides SARIF reports and pre-fuzzing corpus (optional)
      - The `pov`, `povs/`, and `hints/` directories are generated by CRSBench based on experiment configuration
    - Both are installable via pip/uv for easier deployment
  - **No individual `fuzzer.py` per CRS** - all CRS implementations use the same interface
  - May have thin wrappers around the helper interface for convenience, but no per-CRS build/run scripts
  - This standardization simplifies CRS integration and ensures consistency

**Why This Matters**:
- CRSBench doesn't need fuzzer-specific integration code for each CRS
- All CRS implementations must conform to the OSS-Fuzz interface standard
- Build and execution logic is centralized in OSS-Fuzz infrastructure
- Easier to add new CRS implementations without modifying CRSBench core
- Supports both vulnerability discovery (`oss-bugfind-crs`) and patch generation (`oss-bugfix-crs`) workflows

**Implementation Considerations**:
- **Current State**: CRSBench uses `oss-bugfind-crs` (bug finding) and `oss-bugfix-crs` (patch generation) commands
- **Command Format**: Both commands are installable wrappers that provide cleaner interfaces
- **Config Paths**: Use relative paths like `example_configs/ensemble-c` (no `infra/crs/` prefix)
- **Design Principle**: CRS execution logic uses standardized commands for consistent integration

**Reference Documentation**: `docs/ossfuzz-crs-interface.md`


## Standardized CRS interface
The document for standardized CRS interface is in `docs/ossfuzz-crs-interface.md`.

CRSBench will utilize this interface to build CRS docker images and run those
docker images to evaluate CRS.

## Reference projects
Some reference projects are put in `claude_reference_projects`, you should look when asked.

### Docs
Documentation for reference projects is organized in `docs_reference_projects/` by project:

```
docs_reference_projects/
├── fuzzbench/                      # FuzzBench documentation
│   ├── FUZZBENCH-INDEX.md         # Navigation guide and quick lookup
│   ├── FUZZBENCH-OVERVIEW.md      # Master overview and architecture
│   ├── fuzzbench-build-architecture.md  # Build pipeline details
│   ├── fuzzbench-docker-build-process.md  # Docker build process
│   ├── fuzzbench-redis-architecture.md  # Redis infrastructure
│   ├── fuzzbench-snapshots.md     # Snapshot system
│   └── README-fuzzbench.md        # Quick reference
├── patchagent/                     # PatchAgent documentation
│   └── patchagent.md
├── crs-multilang-e2e-eval/        # CRS multilang eval documentation
│   └── crs-multilang-e2e-eval.md
└── scoring-pipeline/              # Scoring pipeline documentation
    └── aixcc-scoring-pipeline-deduplication.md
```

**Guidelines:**
- When documenting a new reference project, create a subdirectory under `docs_reference_projects/`
- Subdirectory name should match the reference project name in `claude_reference_projects/`
- Include a README or index file for multi-document projects

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

#### Import Style
- Use absolute import instead of relative import
- Moving files around for restructuring is straightforward without editing import statements

#### Logging
- Use centralized logger from `crsbench/utils/logger.py`
  ```python
  from crsbench.utils.logger import get_logger
  logger = get_logger(__name__)
  ```

#### Clean Code Principles

**Core Philosophy**: Code is clean if it can be understood easily by everyone on the team.

**General Rules**:
- Follow established conventions in the codebase
- Keep it simple - reduce complexity wherever possible
- Boy Scout Rule: leave code better than you found it
- Fix root causes, not symptoms

**Naming**:
- Use `snake_case` for functions, methods, variables, modules
- Use `PascalCase` for classes
- Use `UPPER_SNAKE_CASE` for constants
- Choose descriptive, unambiguous names that reveal intent
- Use pronounceable, searchable names
- Replace magic numbers with named constants
- Make meaningful distinctions (`get_active_users()` vs `get_users()`)

**Functions**:
- Keep functions short: ideally under 20 lines, max 50 lines
- Single responsibility: one function does one thing
- Minimize parameters (0-2 ideal, max 3-4)
- **Remove unused arguments** - don't prefix with `_` to silence linter warnings
  - If an argument is unused, remove it from the function signature
  - Exception: callback/interface implementations where signature is required
- Avoid boolean flag parameters - split into separate functions
  ```python
  # Bad
  def get_users(include_inactive: bool): ...

  # Good
  def get_active_users(): ...
  def get_all_users(): ...
  ```
- **Always pass boolean values as keyword arguments** (FBT002/FBT003)
  ```python
  # Bad - unclear what True means
  process_data(data, True)
  run_command(cmd, False, True)

  # Good - explicit and readable
  process_data(data, validate=True)
  run_command(cmd, capture_output=False, check=True)
  ```
- **Use `*` to force keyword-only arguments for booleans**
  - Move boolean parameters to the end of the parameter list
  - Place `*` before the boolean parameters to make them keyword-only
  - This keeps other arguments positional (no unnecessary keyword requirement)
  ```python
  # Bad - allows positional boolean args
  def process_data(data, validate: bool = False):
      ...

  # Bad - forces all args to be keyword-only unnecessarily
  def fetch_data(*, url: str, timeout: int, retry: bool = False):
      ...

  # Good - only booleans are keyword-only
  def process_data(data, *, validate: bool = False):
      ...

  def fetch_data(url: str, timeout: int, *, retry: bool = False):
      ...
  ```
- No side effects: function should do what its name says, nothing more
- Use descriptive names: `calculate_monthly_revenue()` not `calc()`

**Indentation and Nesting**:
- Maximum 3 levels of indentation; refactor if deeper
- Use early returns to reduce nesting
  ```python
  # Bad
  def process(data):
      if data:
          if data.is_valid():
              if data.has_items():
                  # deep nesting
                  return result
      return None

  # Good
  def process(data):
      if not data:
          return None
      if not data.is_valid():
          return None
      if not data.has_items():
          return None
      # main logic at minimal indentation
      return result
  ```
- Extract complex conditions into well-named functions
- Extract loop bodies into separate functions when complex

**Comments**:
- Code should be self-documenting; minimize need for comments
- Don't comment what code does - make code clear instead
- Use comments for: intent, clarification of complexity, warnings, TODOs
- Delete commented-out code - version control exists
- Docstrings for public APIs only; skip for obvious internal functions

**Code Organization**:
- Declare variables near their usage
- Group related code together; separate unrelated concepts with blank lines
- Order in classes: class variables → `__init__` → public methods → private methods
- Keep line lengths under 88 characters (configured in ruff)
- Keep files under 300-400 lines; split into multiple modules if larger
  - Extract related functions into separate modules
  - Use `__init__.py` to re-export public APIs for clean imports

**Classes and Modules**:
- Single Responsibility Principle: one reason to change
- Keep classes small with minimal instance variables
- Prefer composition over inheritance
- Law of Demeter: only talk to immediate dependencies
  ```python
  # Bad
  user.get_address().get_city().get_name()

  # Good
  user.get_city_name()
  ```

**Error Handling**:
- Use exceptions rather than return codes
- Don't return `None` for errors - raise exceptions with context
- Be specific with exception types
- Don't use exceptions for flow control

**Code Smells to Avoid**:
- **Rigidity**: small changes require many modifications
- **Fragility**: one change breaks unrelated code
- **Needless Complexity**: over-engineering for hypothetical futures
- **Needless Repetition**: DRY violations (but don't over-abstract)
- **Opacity**: hard-to-understand code
- **Long Parameter Lists**: use dataclasses or TypedDict instead

#### Python-Specific Style

**Pythonic Idioms**:
```python
# Use list comprehensions for simple transformations
names = [user.name for user in users if user.is_active]

# Use enumerate instead of manual index
for i, item in enumerate(items):
    ...

# Use zip for parallel iteration
for name, age in zip(names, ages):
    ...

# Use context managers for resource management
with open(path) as f:
    content = f.read()

# Use f-strings for formatting
message = f"User {name} has {count} items"

# Use pathlib for file paths
from pathlib import Path
config_path = Path(__file__).parent / "config.yaml"
```

**Avoid Anti-patterns**:
```python
# Bad: mutable default argument
def append_to(item, target=[]):
    target.append(item)
    return target

# Good
def append_to(item, target=None):
    if target is None:
        target = []
    target.append(item)
    return target

# Bad: bare except
try:
    risky_operation()
except:
    pass

# Good: specific exception
try:
    risky_operation()
except ValueError as e:
    logger.error(f"Invalid value: {e}")
    raise

# Bad: type checking with type()
if type(obj) == list:
    ...

# Good: use isinstance
if isinstance(obj, list):
    ...
```

**Dataclasses and Pydantic**:
- Use `@dataclass` for simple data containers
- Use Pydantic `BaseModel` for validation and serialization
- Prefer these over plain dicts for structured data

### Testing
- Follow TDD (Test-Driven Development) design when applicable
- **MUST run corresponding tests when modifying a module**
- **ALL test code MUST be in `tests/` directory, NOT in module directories**
- Test files are located in `tests/` directory
- Test file naming: `test_<module_name>.py` (e.g., `test_validation.py`, `test_agent.py`)
- **IMPORTANT: Always use `uv run pytest` instead of bare `pytest`**
  - This ensures tests run in the correct virtual environment
  - Example: `uv run pytest tests/test_<module_name>.py -v`
  - Example: `uv run pytest tests/test_<module_name>.py --cov=crsbench.<module_name>`
- Update tests when changing module behavior or adding features
- **DO NOT use `cat` command or heredocs to create test files**
  - Create proper Python test scripts using the Write tool
  - Use Python's `tempfile` module for temporary test data
  - Use `with open()` context managers for file operations in tests
  - Example: See `test_orchestrator_e2e.py` for proper test file creation

#### Pydantic Validators
- Use Pydantic V2 style validators (migrated from V1)
- **Field validators**: Use `@field_validator` with `@classmethod`
  ```python
  @field_validator('field_name')
  @classmethod
  def validate_field_name(cls, v):
      # validation logic
      return v
  ```
- **Model validators** (cross-field validation): Use `@model_validator(mode='after')`
  ```python
  @model_validator(mode='after')
  def check_mutual_exclusivity(self):
      # validation logic
      return self
  ```
- All validators have been migrated to V2 style - no deprecation warnings

### Type Checking
- **MUST run `just typecheck` after making code changes**
  - This ensures type safety and catches type errors early
  - Example: `just typecheck`
- Use Python's `typing` module for type annotations
  - Use `Optional[T]` for optional types, not `T | None` (for consistency with existing code)
  - Import: `from typing import Optional`
- When working with functions that have Union return types:
  - Use `cast()` from typing module to help the type checker
  - Example: When `run_cmd()` returns `Union[Tuple[str, str], Tuple[str, str, int]]`:
    ```python
    from typing import cast, Tuple

    result = run_cmd(cmd, return_code=True)
    stdout, stderr, exit_code = cast(Tuple[str, str, int], result)
    ```
- **Legacy/auto-generated code exceptions**:
  - `crsbench/hint_generation/sarif_model.py` - Auto-generated SARIF schema (Pydantic V1 style)
  - `crsbench/migration/` - Legacy migration tools
  - Type errors in these files can be ignored unless actively working on them

### Linting and Formatting
- **MUST run `just lint` after making code changes**
  - This catches code style issues and potential bugs
  - Example: `just lint`
- **Use `just lint-fix` to auto-fix issues**
  - Example: `just lint-fix`
- **Use `just format` to format code**
  - Example: `just format`
- **Use `just check` to run all checks (typecheck + lint + format)**
  - Example: `just check`
- Ruff is configured to check:
  - `E`, `W`: pycodestyle errors and warnings
  - `F`: Pyflakes
  - `I`: isort (import sorting)
  - `B`: flake8-bugbear
  - `N`: pep8-naming (enforces naming conventions)
  - `ARG`: flake8-unused-arguments
  - `TD`: flake8-todos (TODO comment format)
  - `TC`: flake8-type-checking (TYPE_CHECKING imports)
  - `ERA`: eradicate (commented-out code detection)
  - `BLE`: flake8-blind-except (no bare except)
  - `FBT`: flake8-boolean-trap (no boolean positional args)
  - `C4`: flake8-comprehensions (prefer comprehensions)
  - `PTH`: flake8-use-pathlib (use pathlib over os.path)
  - `RET`: flake8-return (return statement checks)
  - `PT`: flake8-pytest-style (pytest best practices)
  - `PIE`: flake8-pie (misc lints)
- Line length: 88 characters (configured in ruff)
- **Avoid `# noqa` comments**:
  - `# noqa` should be used only as a last resort when there is absolutely no other way to fix the lint error
  - Before using `# noqa`, try these alternatives first:
    - Remove unused arguments from function signatures
    - Use underscore prefix (`_arg`) for intentionally unused loop variables
    - Refactor code to avoid the lint violation
  - Valid use cases for `# noqa`:
    - Method overrides where parameter names must match the base class (interface compatibility)
    - Auto-generated code that cannot be modified
  - When using `# noqa`, always specify the rule code (e.g., `# noqa: ARG002`)
- **Legacy/auto-generated code exceptions** (same as type checking):
  - `crsbench/hint_generation/sarif_model.py`
  - `crsbench/migration/`

## Documentation Standards
- Create entry in README.md when adding a new component
- Create a README.md to summarize each component in their own directories
- Reference: See `design-docs/` directory for design doc examples

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
