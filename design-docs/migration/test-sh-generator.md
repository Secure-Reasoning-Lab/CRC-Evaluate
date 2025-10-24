# test.sh Generator Design

## Overview
Automated tool to generate `test.sh` functional test scripts for benchmarks that lack them. Uses Claude Agent SDK to analyze project repositories, find unit tests, and generate appropriate test.sh scripts.

## Problem Statement
- Security patch validation requires functional testing beyond vulnerability triggering
- Manual creation of test.sh is time-consuming
- Many benchmarks lack test.sh files
- Need automated approach leveraging existing unit tests in project repositories

## Architecture

### Components
1. **TestFinder Agent**: Discovers unit tests in project repository
2. **DocumentationGenerator Agent**: Creates markdown documentation from discovered tests
3. **TestShGenerator Agent**: Generates test.sh script from documentation
4. **TestShValidator**: Validates test.sh execution in OSS-Fuzz container
5. **CLI Orchestrator**: Coordinates the workflow

### Agent Communication
All agents use Claude Agent SDK with LiteLLM proxy (LITELLM_BASE_URL, LITELLM_API_KEY from environment)

### Data Flow
```
Project Repo → TestFinder → MD Doc → TestShGenerator → test.sh → Validator
                  ↓                        ↓                ↓
            unit_tests.md          test_sh_plan.md    execution.log
```

## Implementation Plan

### 1. TestFinder Agent
**Input**: Project directory path
**Output**: Markdown file listing discovered unit tests with metadata
**Tools**: Read, Grep, Glob, Bash
**Prompt Strategy**:
- Search for test files (test/, tests/, *Test.java, *_test.py, etc.)
- Identify test framework (JUnit, pytest, gtest, etc.)
- List test classes/functions with file paths
- Note build system (Maven, Gradle, CMake, etc.)

### 2. DocumentationGenerator Agent
**Input**: Unit tests markdown, project metadata
**Output**: Structured markdown for test.sh generation
**Tools**: Read
**Prompt Strategy**:
- Analyze test organization
- Identify functional vs unit tests
- Note excluded tests (like root-only tests in Docker)
- Recommend test invocation commands

### 3. TestShGenerator Agent
**Input**: Test documentation markdown
**Output**: test.sh script
**Tools**: Read, Write
**Prompt Strategy**:
- Generate bash script following existing test.sh patterns
- Include build tool invocation (mvn, make, etc.)
- Handle test exclusions (e.g., -Dtest=!TestClass)
- Add appropriate flags (skip coverage, checkstyle, etc.)

### 4. TestShValidator
**Input**: test.sh path, benchmark name
**Output**: Validation report
**Tools**: Bash (infra/helper.py)
**Actions**:
- Build benchmark container
- Execute test.sh inside container
- Capture output and exit code
- Report success/failure

### 5. CLI Orchestrator
**Command**: `crsbench generate-test-sh <benchmark-name>`
**Workflow**:
1. Check if test.sh already exists
2. Locate project repository (from environment or meta.yaml)
3. Run TestFinder agent
4. Run DocumentationGenerator agent
5. Run TestShGenerator agent
6. Run TestShValidator
7. Save test.sh to benchmark directory

## File Locations
- Tool implementation: `crsbench/validation/test_sh_generator.py`
- CLI integration: `crsbench/run_experiment.py` (add subcommand)
- Tests: `tests/test_test_sh_generator.py`
- Generated artifacts: `benchmarks/<name>/test.sh`, `benchmarks/<name>/.aixcc/test_analysis.md`

## Example test.sh Patterns

### Maven (Java)
```bash
#!/bin/bash
MAVEN_ARGS="-Djacoco.skip=true -Drat.skip=true -Dcheckstyle.skip=true \
  -Djavac.target.version=11 -Dtest=!ExcludedTest"
${MVN:-mvn} test $MAVEN_ARGS
```

### Make (C/C++)
```bash
#!/bin/bash
make test
```

### CMake (C/C++)
```bash
#!/bin/bash
mkdir -p build && cd build
cmake .. -DBUILD_TESTING=ON
make test
```

## Environment Requirements
- `LITELLM_BASE_URL`: LiteLLM proxy endpoint
- `LITELLM_API_KEY`: API key for LiteLLM
- `PROJECT_REPO_DIR`: Optional, directory containing cloned project repos

## Future Enhancements (TODO)
- Validate test.sh after patch application
- Support more build systems
- Auto-detect test exclusions needed for Docker environment
- Cache analysis results to avoid re-running agents
