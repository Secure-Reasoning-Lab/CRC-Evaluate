"""
test.sh Generator for CRSBench benchmarks.

This module uses Claude Agent SDK to automatically generate test.sh functional
test scripts for benchmarks that don't have them. It analyzes project repositories,
discovers unit tests, and generates appropriate test.sh scripts.
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Dict, Any, List, Optional
import yaml

from claude_agent_sdk import query, ClaudeAgentOptions


def _get_crsbench_repo_root() -> str:
    """
    Get the crsbench repository root directory.

    This is where .claude/skills/ directory is located for skill loading.

    Returns:
        Absolute path to crsbench repository root
    """
    # This file is at crsbench/migration/test_sh_generator.py
    # Repository root is two levels up
    current_file = Path(__file__).resolve()
    repo_root = current_file.parent.parent.parent
    return str(repo_root)


def _copy_benchmark_files_to_project(
    benchmark_dir: str,
    project_dir: str,
    verbose: bool = False
) -> Optional[str]:
    """
    Copy benchmark's OSS-Fuzz files to project/.oss-fuzz directory.

    This helps the agent understand the build environment and dependencies
    by providing access to build.sh, Dockerfile, harness files, etc.

    Args:
        benchmark_dir: Path to benchmark directory (contains build.sh, Dockerfile, etc.)
        project_dir: Path to project source repository
        verbose: Enable verbose logging

    Returns:
        Path to .oss-fuzz directory in project, or None if copy failed
    """
    oss_fuzz_dir = os.path.join(project_dir, ".oss-fuzz")

    try:
        # Create .oss-fuzz directory
        os.makedirs(oss_fuzz_dir, exist_ok=True)

        # Files to copy from benchmark directory
        files_to_copy = [
            "build.sh",
            "Dockerfile",
            "project.yaml",
        ]

        # Copy files
        copied_files = []
        for filename in files_to_copy:
            src = os.path.join(benchmark_dir, filename)
            dst = os.path.join(oss_fuzz_dir, filename)

            if os.path.exists(src):
                shutil.copy2(src, dst)
                copied_files.append(filename)
                if verbose:
                    print(f"  Copied {filename} to .oss-fuzz/")

        # Copy .aixcc directory (contains harness files metadata)
        aixcc_src = os.path.join(benchmark_dir, ".aixcc")
        aixcc_dst = os.path.join(oss_fuzz_dir, ".aixcc")

        if os.path.exists(aixcc_src):
            if os.path.exists(aixcc_dst):
                shutil.rmtree(aixcc_dst)
            shutil.copytree(aixcc_src, aixcc_dst)
            copied_files.append(".aixcc/")
            if verbose:
                print(f"  Copied .aixcc/ to .oss-fuzz/")

        if verbose and copied_files:
            print(f"✅ Copied OSS-Fuzz files to {oss_fuzz_dir}")

        return oss_fuzz_dir

    except Exception as e:
        if verbose:
            print(f"⚠️  Failed to copy benchmark files: {e}")
        return None


class ShTestGenerator:
    """
    Agent-based test.sh generator using Claude Agent SDK.

    Uses LiteLLM proxy for Claude API access via LITELLM_BASE_URL and LITELLM_API_KEY.
    """

    def __init__(
        self,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929"
    ):
        """
        Initialize the test.sh generator agent.

        Args:
            litellm_base_url: LiteLLM proxy URL (defaults to LITELLM_BASE_URL env var)
            litellm_api_key: LiteLLM API key (defaults to LITELLM_API_KEY env var)
            model: Model to use via LiteLLM
        """
        self.litellm_base_url = (
            litellm_base_url or os.getenv("LITELLM_BASE_URL")
        )
        self.litellm_api_key = (
            litellm_api_key or os.getenv("LITELLM_API_KEY")
        )

        if not self.litellm_base_url:
            raise ValueError(
                "LITELLM_BASE_URL must be set in environment or passed as parameter"
            )
        if not self.litellm_api_key:
            raise ValueError(
                "LITELLM_API_KEY must be set in environment or passed as parameter"
            )

        self.model = model

    def _extract_bash_script(self, text: str) -> Optional[str]:
        """
        Extract the actual bash script from agent response text.

        Agent may include explanations and markdown. This extracts:
        1. Content between ```bash and ``` markers
        2. Or content starting from #!/bin/bash to end of script

        Returns:
            Extracted bash script, or None if not found
        """
        import re

        # Try to find bash code block in markdown
        bash_block_pattern = r'```bash\s*\n(.*?)\n```'
        matches = re.findall(bash_block_pattern, text, re.DOTALL)
        if matches:
            # Return the last (most recent) bash block
            return matches[-1].strip()

        # Try to find content starting with #!/bin/bash
        shebang_pattern = r'(#!/bin/bash.*?)(?=\n\n[#]{2,}|\n\n[A-Z][a-z]+:|\Z)'
        matches = re.findall(shebang_pattern, text, re.DOTALL)
        if matches:
            # Return the longest match (most likely the actual script)
            longest_match = max(matches, key=len)
            return longest_match.strip()

        return None

    def _format_agent_log(
        self,
        messages: List[Any],
        phase_name: str
    ) -> str:
        """
        Format agent messages into readable log.

        Args:
            messages: List of agent messages
            phase_name: Name of the phase (e.g., "Unit Test Discovery")

        Returns:
            Formatted log string
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_lines = [f"\n{'='*70}"]
        log_lines.append(f"Phase: {phase_name}")
        log_lines.append(f"Time: {timestamp}")
        log_lines.append(f"{'='*70}\n")

        for idx, message in enumerate(messages):
            # Add message header with role
            role = getattr(message, 'role', 'unknown')
            log_lines.append(f"\n--- Message {idx + 1} ({role}) ---")

            if hasattr(message, 'content') and message.content:
                if isinstance(message.content, list):
                    for block in message.content:
                        block_type = type(block).__name__

                        # Tool use block (assistant calls a tool)
                        if block_type == "ToolUseBlock":
                            tool_id = getattr(block, 'id', 'unknown')
                            log_lines.append(f"[Tool Call] {block.name} (id: {tool_id})")
                            if hasattr(block, 'input') and block.input:
                                # Format input nicely
                                input_str = json.dumps(block.input, indent=2) if isinstance(block.input, dict) else str(block.input)
                                log_lines.append(f"  Input: {input_str}")

                        # Tool result block (user/system returns tool result)
                        elif block_type == "ToolResultBlock":
                            tool_id = getattr(block, 'tool_use_id', 'unknown')
                            log_lines.append(f"[Tool Result] (id: {tool_id})")
                            if hasattr(block, 'content'):
                                content = str(block.content)[:500]  # Limit to 500 chars
                                if len(str(block.content)) > 500:
                                    content += "... (truncated)"
                                log_lines.append(f"  Result: {content}")

                        # Text block (agent thinking/response)
                        elif hasattr(block, 'text'):
                            log_lines.append(f"[Text]\n{block.text}\n")

                elif isinstance(message.content, str):
                    log_lines.append(f"[Text]\n{message.content}\n")

        log_lines.append("")
        return "\n".join(log_lines)

    async def find_unit_tests(
        self,
        project_dir: str,
        benchmark_dir: str,
        verbose: bool = False
    ) -> tuple[str, str]:
        """
        Find unit tests in the project repository.

        Args:
            project_dir: Directory containing the project source code
            benchmark_dir: Directory containing benchmark files (.aixcc/, etc.)
            verbose: Enable verbose logging

        Returns:
            Tuple of (markdown_document, agent_log)
        """
        prompt = f"""Analyze the project repository at `{project_dir}` and identify all unit tests and functional tests.

This is an OSS-Fuzz project with `.oss-fuzz/` directory containing build.sh, Dockerfile, and .aixcc/meta.yaml.

**IMPORTANT**: Benchmark directory is `{benchmark_dir}`. Save your analysis to `{benchmark_dir}/.agent/test_analysis.md`.

## CRITICAL: Purpose of test.sh

**test.sh is for FUNCTIONALITY TESTING (regression testing), NOT for build verification.**
- The goal is to run tests that verify the project's functionality works correctly
- These tests should be able to detect when code changes break existing behavior
- We need EXISTING unit tests from the project, not smoke tests or build checks

## CRITICAL: Test Discovery Requirements

**You MUST exhaustively search for existing test suites. Smoke tests are a LAST RESORT.**

Search for test infrastructure in this priority order:
1. **Build system test targets**:
   - Maven: pom.xml with surefire-plugin, `mvn test` targets
   - Gradle: build.gradle with test task, `gradle test`
   - CMake: CMakeLists.txt with enable_testing(), add_test()
   - Makefile: `make check`, `make test` targets
   - Autotools: configure.ac with AC_CONFIG_TESTDIR

2. **Test framework configuration files**:
   - Python: pytest.ini, setup.cfg [tool.pytest], pyproject.toml, tox.ini
   - Java: src/test/java/, testng.xml
   - JavaScript: jest.config.js, mocha.opts, package.json scripts.test
   - Go: *_test.go files
   - Rust: tests/ directory, #[test] in lib.rs

3. **Test directories**:
   - tests/, test/, t/, spec/, __tests__/
   - src/test/, src/tests/
   - integration/, e2e/

**DO NOT assume a project has no tests just because they are not immediately obvious.**
Many projects have tests in non-standard locations or use custom test frameworks.

Please use the appropriate skill to:
1. Identify the build system and test framework
2. Find all unit test files (search EXHAUSTIVELY)
3. Document test commands and exclusions
4. Generate a comprehensive patch exclude list
5. **Save the analysis to `{benchmark_dir}/.agent/test_analysis.md` using Write tool**

If NO tests are found after exhaustive search, document:
- All locations searched
- All configuration files examined
- Why you concluded no tests exist

Provide your analysis as a markdown document with all required sections.
"""

        # Use repo root as cwd so skills can be loaded from .claude/skills/
        # The agent can still access project files using absolute paths
        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Skill"],
            setting_sources=["project"],
            cwd=_get_crsbench_repo_root(),
            system_prompt=(
                "You are a thorough software testing analyst specializing in finding existing unit tests. "
                "Your goal is to find EXISTING unit/functional tests in projects - NOT to create smoke tests. "
                "\n\n"
                "**CRITICAL PRIORITY**: "
                "1. FIRST: Find existing unit tests (mvn test, pytest, make check, etc.) "
                "2. SECOND: Find integration/functional tests if no unit tests exist "
                "3. LAST RESORT ONLY: Smoke tests (only after exhaustive search proves no tests exist) "
                "\n\n"
                "Use available skills to help with your analysis. "
                "Use Grep to search patterns, Glob to find files, Read to examine files, Write to create files, and Edit to modify files. "
                "You can use WebSearch to research build systems/frameworks, WebFetch for documentation, "
                "and TodoWrite to track your analysis progress. "
                "You can use Bash for file system exploration but **CRITICAL**: "
                "- Use SIMPLE Bash commands only (ls, find, file, cat, head, tail, wc, grep, etc.) "
                "- AVOID complex commands with pipes, redirects, or multiple operations "
                "- DO NOT run BUILD or TEST commands (mvn compile, mvn test, make, pytest, etc.) "
                "- This is STATIC ANALYSIS only - analyze code structure, do not execute code. "
                "Be systematic and cite specific file paths."
            ),
            env={
                "ANTHROPIC_BASE_URL": self.litellm_base_url,
                "ANTHROPIC_AUTH_TOKEN": self.litellm_api_key,
            }
        )

        result_text = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    # Print message with role
                    role = getattr(message, 'role', 'unknown')
                    print(f"\n{'='*60}")
                    print(f"📨 Message (role: {role})")
                    print(f"{'='*60}")

                    if hasattr(message, 'content') and message.content:
                        # Print verbose info for debugging
                        if isinstance(message.content, list):
                            for block in message.content:
                                block_type = type(block).__name__

                                # Tool use block
                                if block_type == "ToolUseBlock":
                                    tool_id = getattr(block, 'id', 'unknown')
                                    print(f"🔧 [Tool Call] {block.name} (id: {tool_id})")
                                    if hasattr(block, 'input'):
                                        import json
                                        input_str = json.dumps(block.input, indent=2) if isinstance(block.input, dict) else str(block.input)
                                        print(f"   Input: {input_str}")

                                # Tool result block
                                elif block_type == "ToolResultBlock":
                                    tool_id = getattr(block, 'tool_use_id', 'unknown')
                                    print(f"✅ [Tool Result] (id: {tool_id})")
                                    if hasattr(block, 'content'):
                                        content = str(block.content)[:200]
                                        if len(str(block.content)) > 200:
                                            content += "... (truncated)"
                                        print(f"   Result: {content}")

                                # Text block
                                elif hasattr(block, 'text'):
                                    preview = block.text[:200]
                                    if len(block.text) > 200:
                                        preview += "..."
                                    print(f"💬 [Agent Response]\n{preview}\n")

                        elif isinstance(message.content, str):
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            print(f"💬 [Agent Response]\n{preview}\n")

        except Exception as e:
            if verbose:
                print(f"❌ TestFinder agent error: {e}")
                import traceback
                traceback.print_exc()
            result_text = f"# Error\n\nFailed to analyze project: {str(e)}"

        # Extract text from last assistant message only
        if not result_text:  # Only if no error occurred
            for message in reversed(messages):
                if getattr(message, 'role', None) == 'assistant':
                    if hasattr(message, 'content') and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                if hasattr(block, 'text'):
                                    result_text += block.text
                        elif isinstance(message.content, str):
                            result_text = message.content
                    break

        # Generate agent log
        agent_log = self._format_agent_log(messages, "Unit Test Discovery")

        return result_text, agent_log

    async def generate_test_sh_script(
        self,
        test_analysis_md: str,
        benchmark_name: str,
        benchmark_dir: str,
        with_docker_testing: bool = False,
        verbose: bool = False
    ) -> tuple[str, str, str]:
        """
        Generate test.sh script from test analysis markdown.

        Args:
            test_analysis_md: Markdown document from find_unit_tests()
            benchmark_name: Name of the benchmark
            benchmark_dir: Path to benchmark directory
            with_docker_testing: Enable iterative Docker testing and refinement
            verbose: Enable verbose logging

        Returns:
            Tuple of (script_content, agent_response_text, agent_log)
            - script_content: Extracted bash script
            - agent_response_text: Full agent response (rationale, explanations)
            - agent_log: Formatted agent conversation log
        """
        if with_docker_testing:
            # Iterative approach with Docker testing
            prompt = f"""Generate BOTH test.sh and bad_patch.diff for benchmark `{benchmark_name}`.

# Context
- Benchmark directory: {benchmark_dir}
- **CRITICAL**: test.sh file is located at /src/test.sh in the container
- **CRITICAL**: test.sh is executed from WORKDIR (usually /src/<project-name>)
- **CRITICAL**: Project source code is mounted at WORKDIR, so test.sh runs in the project root
- Example: If WORKDIR=/src/libxml2, test.sh runs `bash /src/test.sh` from /src/libxml2

# CRITICAL: Build Environment Assumptions
**test.sh runs in a CLEAN environment with NO pre-built artifacts.**

## What is NOT available:
- `/out` directory is NOT mounted (no fuzzer binaries)
- `/work` directory is NOT mounted (no build cache)
- ALL previous build artifacts are DELETED before test.sh runs
- The source code is mounted FRESH (completely unbuilt state)

## What test.sh MUST do:
1. **BUILD the project from scratch** before running any tests
2. For C/C++: run `./configure && make` or `cmake .. && make` first
3. For Java/Maven: run `mvn compile` or `mvn package` before `mvn test`
4. For Java/Gradle: run `gradle build` or `gradle compileJava` before `gradle test`
5. For Python: install dependencies if needed (`pip install -e .`)

## Common Mistakes to AVOID:
- ❌ Assuming binaries exist in `/out/` - they DON'T
- ❌ Assuming `make test` will work without `make` first
- ❌ Assuming Maven test will work without compile first
- ❌ Skipping the build step entirely

# Test Analysis
```markdown
{test_analysis_md}
```

# Your Task - THREE PHASES
Use the appropriate skills to complete all tasks:

## Task 1: Generate test.sh (use test-sh-generator-docker skill)
1. Generate an initial test.sh script based on the analysis
2. Build the Docker image and test the script iteratively
3. Refine the script until it runs successfully in Docker
4. Save test.sh to {benchmark_dir}/test.sh
5. **Save rationale to {benchmark_dir}/.agent/test_sh_rationale.md**

**CRITICAL**: You MUST keep iterating until mcp__crsbench__check_test_sh returns success.

## Task 2: Generate bad_patch.diff (use bad-patch-generator skill)
1. Analyze the test suite to identify tested functionality
2. Generate 2-3 HIGH PRIORITY mutations (dummy returns, removed calls)
3. Create bad_patch.diff that compiles but breaks tests
4. Save bad_patch.diff to {benchmark_dir}/bad_patch.diff

**CRITICAL**: Use HIGH PRIORITY mutations (dummy returns, skip function calls) to guarantee test failures.

## Task 3: Verify bad_patch.diff breaks test.sh
1. Use mcp__crsbench__verify_bad_patch to verify the patch
2. Check if test.sh FAILS with bad_patch applied (expected behavior)
3. If test.sh PASSES with bad_patch:
   - This means EITHER bad_patch is too weak OR test.sh doesn't cover the mutated code
   - **FIRST OPTION**: Regenerate STRONGER bad_patch.diff (more aggressive mutations)
     - Mutate more critical functions
     - Use more dummy returns
   - **ONLY AS LAST RESORT**: Add more tests to test.sh (if existing tests are insufficient)
   - Re-verify until bad_patch causes test failure
4. Keep iterating until verification succeeds

**CRITICAL - NO OVERFITTING**:
- test.sh should run ONLY existing project tests, not custom bad_patch checks
- Do NOT add special logic like "if X returns NULL, fail" to test.sh
- bad_patch should break existing tests naturally
- Prefer making bad_patch stronger over modifying test.sh

## CRITICAL: test.sh Purpose and Test Selection Strategy

**PURPOSE**: test.sh is for FUNCTIONALITY TESTING (regression testing), NOT for build verification.
- test.sh should verify that the project's functionality works correctly
- test.sh should detect when code changes break existing behavior
- test.sh is NOT for checking if the build completes successfully

**TEST SELECTION PRIORITY** (follow this order strictly):
1. **FIRST PRIORITY - Existing Unit Tests**: Always prefer running the project's existing unit tests
   - Maven: `mvn test`, `mvn surefire:test`
   - Gradle: `gradle test`, `./gradlew test`
   - Python: `pytest`, `python -m unittest`
   - C/C++: `make check`, `ctest`, `make test`
   - Go: `go test ./...`
   - These tests are designed to validate functionality and detect regressions

2. **SECOND PRIORITY - Integration/Functional Tests**: If unit tests don't exist, look for integration tests
   - Look for `tests/`, `test/`, `e2e/`, `integration/` directories
   - Check for test runners in package.json, Makefile, etc.

3. **LAST RESORT ONLY - Smoke Tests**: Create smoke tests ONLY when:
   - The project has ABSOLUTELY NO existing test suite (no test framework, no test files)
   - You have exhaustively searched for tests and found none
   - Document why no tests were found in the rationale
   - Smoke tests should still validate FUNCTIONALITY, not just that binaries exist

**WARNING**: Do NOT default to smoke tests. Many projects have test suites that may not be obvious.
Search thoroughly for: CMakeLists.txt with enable_testing(), Makefile with test targets, pytest.ini,
setup.py with test_suite, pom.xml with surefire, build.gradle with test tasks, etc.

**IF NO UNIT TESTS EXIST**:
- ONLY after exhaustive search, output a minimal test.sh:
  ```bash
  #!/bin/bash
  echo "No unit tests available for this project"
  exit 0
  ```
- In this case, skip bad_patch.diff generation (no tests to break)
- Document the search process in rationale (what was searched, why no tests found)

**CRITICAL**: You MUST verify bad_patch.diff and ensure test.sh fails with the patch applied.

Complete ALL THREE tasks before finishing.
"""
        else:
            # Simple two-phase approach (analyze → generate)
            prompt = f"""Generate BOTH test.sh and bad_patch.diff for benchmark `{benchmark_name}`.

# Context
- Benchmark directory: {benchmark_dir}
- **CRITICAL**: test.sh file is located at /src/test.sh in the container
- **CRITICAL**: test.sh is executed from WORKDIR (usually /src/<project-name>)
- **CRITICAL**: Project source code is mounted at WORKDIR, so test.sh runs in the project root
- Example: If WORKDIR=/src/libxml2, test.sh runs `bash /src/test.sh` from /src/libxml2

# CRITICAL: Build Environment Assumptions
**test.sh runs in a CLEAN environment with NO pre-built artifacts.**

## What is NOT available:
- `/out` directory is NOT mounted (no fuzzer binaries)
- `/work` directory is NOT mounted (no build cache)
- ALL previous build artifacts are DELETED before test.sh runs
- The source code is mounted FRESH (completely unbuilt state)

## What test.sh MUST do:
1. **BUILD the project from scratch** before running any tests
2. For C/C++: run `./configure && make` or `cmake .. && make` first
3. For Java/Maven: run `mvn compile` or `mvn package` before `mvn test`
4. For Java/Gradle: run `gradle build` or `gradle compileJava` before `gradle test`
5. For Python: install dependencies if needed (`pip install -e .`)

## Common Mistakes to AVOID:
- ❌ Assuming binaries exist in `/out/` - they DON'T
- ❌ Assuming `make test` will work without `make` first
- ❌ Assuming Maven test will work without compile first
- ❌ Skipping the build step entirely

# Test Analysis
```markdown
{test_analysis_md}
```

# Your Task - TWO PHASES
Use the appropriate skills to complete both tasks:

## Task 1: Generate test.sh (use test-sh-generator-simple skill)
1. Generate a working test.sh script based on the test analysis
2. Save test.sh to {benchmark_dir}/test.sh
3. **Save rationale to {benchmark_dir}/.agent/test_sh_rationale.md**
4. Output the bash script content for verification

## Task 2: Generate bad_patch.diff (use bad-patch-generator skill)
1. Analyze the test suite to identify tested functionality
2. Generate 2-3 HIGH PRIORITY mutations (dummy returns, removed calls)
3. Create bad_patch.diff that compiles but breaks tests
4. Save bad_patch.diff to {benchmark_dir}/bad_patch.diff

**CRITICAL**: Use HIGH PRIORITY mutations (dummy returns, skip function calls) to guarantee test failures.

**CRITICAL - NO OVERFITTING**:
- bad_patch should break existing unit tests that test.sh runs
- Do NOT expect test.sh to have custom checks for your mutations
- Mutations should break actual functionality that existing tests validate

## CRITICAL: test.sh Purpose and Test Selection Strategy

**PURPOSE**: test.sh is for FUNCTIONALITY TESTING (regression testing), NOT for build verification.
- test.sh should verify that the project's functionality works correctly
- test.sh should detect when code changes break existing behavior
- test.sh is NOT for checking if the build completes successfully

**TEST SELECTION PRIORITY** (follow this order strictly):
1. **FIRST PRIORITY - Existing Unit Tests**: Always prefer running the project's existing unit tests
   - Maven: `mvn test`, `mvn surefire:test`
   - Gradle: `gradle test`, `./gradlew test`
   - Python: `pytest`, `python -m unittest`
   - C/C++: `make check`, `ctest`, `make test`
   - Go: `go test ./...`
   - These tests are designed to validate functionality and detect regressions

2. **SECOND PRIORITY - Integration/Functional Tests**: If unit tests don't exist, look for integration tests
   - Look for `tests/`, `test/`, `e2e/`, `integration/` directories
   - Check for test runners in package.json, Makefile, etc.

3. **LAST RESORT ONLY - Smoke Tests**: Create smoke tests ONLY when:
   - The project has ABSOLUTELY NO existing test suite (no test framework, no test files)
   - You have exhaustively searched for tests and found none
   - Document why no tests were found in the rationale
   - Smoke tests should still validate FUNCTIONALITY, not just that binaries exist

**WARNING**: Do NOT default to smoke tests. Many projects have test suites that may not be obvious.
Search thoroughly for: CMakeLists.txt with enable_testing(), Makefile with test targets, pytest.ini,
setup.py with test_suite, pom.xml with surefire, build.gradle with test tasks, etc.

**IF NO UNIT TESTS EXIST**:
- ONLY after exhaustive search, output a minimal test.sh:
  ```bash
  #!/bin/bash
  echo "No unit tests available for this project"
  exit 0
  ```
- In this case, skip bad_patch.diff generation (no tests to break)
- Document the search process in rationale (what was searched, why no tests found)

Complete BOTH tasks before finishing.
"""

        # Configure tools based on mode
        if with_docker_testing:
            from pathlib import Path
            mcp_server_script = Path(__file__).parent / "crsbench_mcp_server.py"

            allowed_tools = [
                "Read", "Write", "Edit", "Grep", "Glob", "Bash",
                "WebSearch", "WebFetch", "TodoWrite", "Skill",
                # MCP tools for Docker operations
                "mcp__crsbench__build_benchmark",
                "mcp__crsbench__check_test_sh",
                "mcp__crsbench__run_command_in_container",
                "mcp__crsbench__get_benchmark_info",
                "mcp__crsbench__verify_bad_patch"
            ]

            mcp_servers = {
                "crsbench": {
                    "command": "python3",
                    "args": [str(mcp_server_script), benchmark_name]
                }
            }

            system_prompt = (
                "You are an expert test.sh script generator with access to Docker build and test tools. "
                "Use available skills to help with test.sh generation. "
                "\n\n"
                "**CRITICAL: test.sh PURPOSE**\n"
                "test.sh is for FUNCTIONALITY TESTING (regression testing), NOT for build verification.\n"
                "- test.sh should run the project's EXISTING unit tests\n"
                "- test.sh should detect when code changes break existing behavior\n"
                "- test.sh is NOT just for checking if the build completes\n"
                "\n"
                "**CRITICAL: TEST PRIORITY**\n"
                "1. FIRST: Use existing unit tests (mvn test, pytest, make check, gradle test, etc.)\n"
                "2. SECOND: Use integration/functional tests if no unit tests\n"
                "3. LAST RESORT ONLY: Smoke tests (only after exhaustive search proves no tests exist)\n"
                "DO NOT create smoke tests if unit tests exist in the project!\n"
                "\n"
                "**CRITICAL OUTPUT REQUIREMENT:**\n"
                "- Your final output MUST be an EXECUTABLE BASH SCRIPT\n"
                "- DO NOT output markdown, explanations, or analysis documents\n"
                "- The script must run successfully inside Docker container\n"
                "- DO NOT finish until the script executes successfully\n"
                "\n"
                "**CRITICAL EXECUTION ENVIRONMENT:**\n"
                "- **test.sh file is located at /src/test.sh in the container**\n"
                "- **test.sh is executed from WORKDIR (usually /src/<project-name>)**\n"
                "- **Project source code is mounted at WORKDIR, so test.sh runs in the project root**\n"
                "- **Static analysis** (Read, Grep, Glob, Bash for file exploration): Run on HOST\n"
                "- **Build execution** (mvn compile, make, cmake, etc.): MUST run in DOCKER container\n"
                "- **Test execution** (mvn test, pytest, make test, etc.): MUST run in DOCKER container\n"
                "- You MUST use MCP tools (mcp__crsbench__*) for ALL build/test execution\n"
                "- DO NOT use Bash tool to execute build/test commands - use MCP tools for that\n"
                "- Bash is OK for file exploration (ls, find, cat) but NOT for executing builds/tests\n"
                "- Use mcp__crsbench__* tools exclusively for Docker operations\n"
                "\n\n"
                "Available MCP tools:\n"
                "- mcp__crsbench__build_benchmark: Build Docker image for benchmark using OSS-Fuzz helper.py. Returns dict with 'success' (bool) and 'logs' (str) keys\n"
                "- mcp__crsbench__check_test_sh: Test the test.sh script in Docker container. Returns execution logs\n"
                "- mcp__crsbench__run_command_in_container: Run arbitrary commands inside Docker container (e.g., 'which sbt', 'mvn --version')\n"
                "- mcp__crsbench__get_benchmark_info: Get benchmark metadata\n"
                "- mcp__crsbench__verify_bad_patch: Apply bad_patch.diff, run test.sh, check if it fails, then restore. Returns dict with 'valid' (bool), 'test_passed' (bool), 'patch_applied' (bool), 'output' (str)\n"
                "\n"
                "For file operations, use Read/Write/Edit/Grep/Glob. "
                "Use Write to create new test.sh scripts, Edit to modify existing scripts. "
                "For research, use WebSearch/WebFetch. "
                "Use TodoWrite to track your iterative refinement process. "
                "\n\n"
                "**YOU MUST NOT FINISH THIS TASK UNTIL test.sh EXECUTES SUCCESSFULLY IN DOCKER**"
            )
        else:
            allowed_tools = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Skill"]
            mcp_servers = None
            system_prompt = (
                "You are a bash scripting expert specializing in running existing project test suites. "
                "Use available skills to help with test.sh generation. "
                "Generate clean, working bash scripts following the patterns provided. "
                "\n\n"
                "**CRITICAL: test.sh PURPOSE**\n"
                "test.sh is for FUNCTIONALITY TESTING (regression testing), NOT for build verification.\n"
                "- test.sh should run the project's EXISTING unit tests\n"
                "- test.sh should detect when code changes break existing behavior\n"
                "- test.sh is NOT just for checking if the build completes\n"
                "\n"
                "**CRITICAL: TEST PRIORITY**\n"
                "1. FIRST: Use existing unit tests (mvn test, pytest, make check, gradle test, etc.)\n"
                "2. SECOND: Use integration/functional tests if no unit tests\n"
                "3. LAST RESORT ONLY: Smoke tests (only after exhaustive search proves no tests exist)\n"
                "DO NOT create smoke tests if unit tests exist in the project!\n"
                "\n"
                "You can use Read to examine files, Write to create files, Edit to modify files, "
                "WebSearch to research build system patterns, WebFetch for official docs, "
                "and TodoWrite to organize your work. "
                "\n\n"
                "**CRITICAL EXECUTION ENVIRONMENT:**\n"
                "- **test.sh file is located at /src/test.sh in the container**\n"
                "- **test.sh is executed from WORKDIR (usually /src/<project-name>)**\n"
                "- **Project source code is mounted at WORKDIR, so test.sh runs in the project root**\n"
                "\n"
                "Output only the script content, no extra text."
            )

        # Use repo root as cwd so skills can be loaded from .claude/skills/
        # The agent can still access benchmark files using absolute paths
        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            setting_sources=["project"],
            cwd=_get_crsbench_repo_root(),
            system_prompt=system_prompt,
            env={
                "ANTHROPIC_BASE_URL": self.litellm_base_url,
                "ANTHROPIC_AUTH_TOKEN": self.litellm_api_key,
            }
        )

        script_content = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    # Print message with role
                    role = getattr(message, 'role', 'unknown')
                    print(f"\n{'='*60}")
                    print(f"📨 Message (role: {role})")
                    print(f"{'='*60}")

                    if hasattr(message, 'content') and message.content:
                        # Print verbose info for debugging
                        if isinstance(message.content, list):
                            for block in message.content:
                                block_type = type(block).__name__

                                # Tool use block
                                if block_type == "ToolUseBlock":
                                    tool_id = getattr(block, 'id', 'unknown')
                                    print(f"🔧 [Tool Call] {block.name} (id: {tool_id})")
                                    if hasattr(block, 'input'):
                                        import json
                                        input_str = json.dumps(block.input, indent=2) if isinstance(block.input, dict) else str(block.input)
                                        print(f"   Input: {input_str}")

                                # Tool result block
                                elif block_type == "ToolResultBlock":
                                    tool_id = getattr(block, 'tool_use_id', 'unknown')
                                    print(f"✅ [Tool Result] (id: {tool_id})")
                                    if hasattr(block, 'content'):
                                        content = str(block.content)[:200]
                                        if len(str(block.content)) > 200:
                                            content += "... (truncated)"
                                        print(f"   Result: {content}")

                                # Text block
                                elif hasattr(block, 'text'):
                                    preview = block.text[:200]
                                    if len(block.text) > 200:
                                        preview += "..."
                                    print(f"💬 [Agent Response]\n{preview}\n")

                        elif isinstance(message.content, str):
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            print(f"💬 [Agent Response]\n{preview}\n")

        except Exception as e:
            if verbose:
                print(f"❌ TestShGenerator agent error: {e}")
                import traceback
                traceback.print_exc()
            # Fallback script
            script_content = """#!/bin/bash
# Auto-generated fallback test.sh
# Failed to generate specific test script
echo "Error: Test script generation failed"
exit 1
"""

        # Extract text from last assistant message only
        if not script_content:  # Only if no error occurred
            for message in reversed(messages):
                if getattr(message, 'role', None) == 'assistant':
                    if hasattr(message, 'content') and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                if hasattr(block, 'text'):
                                    script_content += block.text
                        elif isinstance(message.content, str):
                            script_content = message.content
                    break

        # Save original agent response text (includes rationale, explanations)
        agent_response_text = script_content.strip()

        # Extract actual bash script from agent response
        # Agent may include explanations, so we need to extract the script
        extracted_script = self._extract_bash_script(script_content)
        if extracted_script:
            script_content = extracted_script
        else:
            # If extraction fails, try to clean up the original content
            script_content = script_content.strip()

        # Remove markdown code fences if present
        if script_content.startswith("```bash"):
            script_content = script_content[7:]
        if script_content.startswith("```"):
            script_content = script_content[3:]
        if script_content.endswith("```"):
            script_content = script_content[:-3]

        script_content = script_content.strip()

        # Ensure it starts with shebang
        if not script_content.startswith("#!/bin/bash"):
            script_content = "#!/bin/bash\n\n" + script_content

        # Generate agent log
        agent_log = self._format_agent_log(messages, "Test Script Generation")

        return script_content, agent_response_text, agent_log

    def find_unit_tests_sync(
        self,
        project_dir: str,
        benchmark_dir: str,
        verbose: bool = False
    ) -> tuple[str, str]:
        """Synchronous wrapper for find_unit_tests.

        Returns:
            Tuple of (markdown_document, agent_log)
        """
        return asyncio.run(self.find_unit_tests(project_dir, benchmark_dir, verbose))

    def generate_test_sh_script_sync(
        self,
        test_analysis_md: str,
        benchmark_name: str,
        benchmark_dir: str,
        with_docker_testing: bool = False,
        verbose: bool = False
    ) -> tuple[str, str, str]:
        """Synchronous wrapper for generate_test_sh_script.

        Returns:
            Tuple of (script_content, agent_response_text, agent_log)
        """
        return asyncio.run(
            self.generate_test_sh_script(
                test_analysis_md,
                benchmark_name,
                benchmark_dir,
                with_docker_testing,
                verbose
            )
        )

    async def generate_bad_patch(
        self,
        test_analysis_md: str,
        project_dir: str,
        benchmark_dir: str,
        verbose: bool = False
    ) -> tuple[str, str, str]:
        """
        Generate bad_patch.diff that breaks functionality without compilation errors.

        Args:
            test_analysis_md: Markdown document from find_unit_tests()
            project_dir: Path to project source repository
            benchmark_dir: Path to benchmark directory
            verbose: Enable verbose logging

        Returns:
            Tuple of (patch_content, agent_response_text, agent_log)
            - patch_content: Extracted diff content
            - agent_response_text: Full agent response (rationale, explanations)
            - agent_log: Formatted agent conversation log
        """
        prompt = f"""Generate a bad_patch.diff file that breaks functionality without causing compilation errors.

# Context
- Project directory: {project_dir}
- Benchmark directory: {benchmark_dir}
- The patch should apply cleanly but cause test.sh to fail

# Test Analysis
```markdown
{test_analysis_md}
```

# Your Task
Use the appropriate skill to:
1. Analyze the test suite and identify what functionality is being tested
2. Find source files that implement this tested functionality
3. Generate 3-5 semantic mutations that will cause test failures
4. Create a unified diff (bad_patch.diff) with these mutations
5. Ensure the patch compiles but breaks functionality

**CRITICAL**: The patch must:
- Compile successfully (no syntax errors)
- Break functionality tested by test.sh (tests will fail)
- Make semantic changes (wrong logic, not syntax errors)

Output the final bad_patch.diff content.
"""

        # Use repo root as cwd so skills can be loaded from .claude/skills/
        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Skill"],
            setting_sources=["project"],
            cwd=_get_crsbench_repo_root(),
            system_prompt=(
                "You are an expert code mutation specialist. "
                "Use available skills to help with bad patch generation. "
                "You can use Read to examine source files, Grep to search patterns, "
                "Glob to find files, Write to create the patch file, "
                "and Edit to modify files if needed. "
                "Use TodoWrite to track your mutation strategy. "
                "\n\n"
                "**CRITICAL OUTPUT REQUIREMENT:**\n"
                "- Your final output should be a valid unified diff format\n"
                "- The patch must compile but break functionality\n"
                "- Target files that are tested by test.sh\n"
                "- Make semantic errors (wrong logic), not syntax errors\n"
            ),
            env={
                "ANTHROPIC_BASE_URL": self.litellm_base_url,
                "ANTHROPIC_AUTH_TOKEN": self.litellm_api_key,
            }
        )

        patch_content = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    # Print message with role
                    role = getattr(message, 'role', 'unknown')
                    print(f"\n{'='*60}")
                    print(f"📨 Message (role: {role})")
                    print(f"{'='*60}")

                if hasattr(message, 'content') and message.content:
                    # Extract text content and print verbose info
                    if isinstance(message.content, list):
                        for block in message.content:
                            block_type = type(block).__name__

                            # Tool use block
                            if block_type == "ToolUseBlock":
                                if verbose:
                                    tool_id = getattr(block, 'id', 'unknown')
                                    print(f"🔧 [Tool Call] {block.name} (id: {tool_id})")
                                    if hasattr(block, 'input'):
                                        import json
                                        input_str = json.dumps(block.input, indent=2) if isinstance(block.input, dict) else str(block.input)
                                        print(f"   Input: {input_str}")

                            # Tool result block
                            elif block_type == "ToolResultBlock":
                                if verbose:
                                    tool_id = getattr(block, 'tool_use_id', 'unknown')
                                    print(f"✅ [Tool Result] (id: {tool_id})")
                                    if hasattr(block, 'content'):
                                        content = str(block.content)[:200]
                                        if len(str(block.content)) > 200:
                                            content += "... (truncated)"
                                        print(f"   Result: {content}")

                            # Text block
                            elif hasattr(block, 'text'):
                                patch_content += block.text
                                if verbose:
                                    # Print first 200 chars of agent response
                                    preview = block.text[:200]
                                    if len(block.text) > 200:
                                        preview += "..."
                                    print(f"💬 [Agent Response]\n{preview}\n")

                    elif isinstance(message.content, str):
                        patch_content += message.content
                        if verbose:
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            print(f"💬 [Agent Response]\n{preview}\n")

        except Exception as e:
            if verbose:
                print(f"❌ BadPatchGenerator agent error: {e}")
                import traceback
                traceback.print_exc()
            patch_content = f"# Error generating bad patch: {str(e)}"

        # Save original agent response text
        agent_response_text = patch_content.strip()

        # Extract actual diff from agent response (may be in markdown code block)
        import re
        diff_block_pattern = r'```diff\s*\n(.*?)\n```'
        matches = re.findall(diff_block_pattern, patch_content, re.DOTALL)
        if matches:
            patch_content = matches[-1].strip()
        else:
            # Try to find content starting with "diff --git"
            diff_start = patch_content.find("diff --git")
            if diff_start != -1:
                patch_content = patch_content[diff_start:].strip()

        # Generate agent log
        agent_log = self._format_agent_log(messages, "Bad Patch Generation")

        return patch_content, agent_response_text, agent_log

    def generate_bad_patch_sync(
        self,
        test_analysis_md: str,
        project_dir: str,
        benchmark_dir: str,
        verbose: bool = False
    ) -> tuple[str, str, str]:
        """Synchronous wrapper for generate_bad_patch.

        Returns:
            Tuple of (patch_content, agent_response_text, agent_log)
        """
        return asyncio.run(
            self.generate_bad_patch(
                test_analysis_md,
                project_dir,
                benchmark_dir,
                verbose
            )
        )


def _add_generation_header(
    script_content: str,
    benchmark_name: str,
    with_docker_testing: bool = False
) -> str:
    """
    Add generation header comment to test.sh script.

    Args:
        script_content: Original test.sh script content
        benchmark_name: Name of the benchmark
        with_docker_testing: Whether Docker testing was used

    Returns:
        Script content with header comment added
    """
    # Generate timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Determine generation method
    method = "iterative Docker testing (MCP-enhanced)" if with_docker_testing else "two-phase analysis"

    # Create header comment
    header = f"""#!/bin/bash
#
# Auto-generated test.sh for CRSBench benchmark
#
# Generated by: Claude Agent SDK (test.sh generator)
# Benchmark: {benchmark_name}
# Method: {method}
# Generated at: {timestamp}
#
# This script runs functional/unit tests for the project.
# For analysis details, see .aixcc/test_analysis.md
#

"""

    # Remove original shebang if present
    if script_content.startswith("#!/bin/bash"):
        # Find the end of the first line
        first_newline = script_content.find("\n")
        if first_newline != -1:
            # Remove shebang and leading empty lines
            script_content = script_content[first_newline + 1:].lstrip("\n")

    # Combine header with script content
    return header + script_content


def generate_test_sh_for_benchmark(
    benchmark_name: str,
    benchmark_dir: str,
    project_dir: str,
    output_path: Optional[str] = None,
    litellm_base_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
    with_docker_testing: bool = True,
    model: str = "claude-sonnet-4-5-20250929",
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Generate test.sh and bad_patch.diff for a benchmark.

    Args:
        benchmark_name: Name of the benchmark
        benchmark_dir: Path to benchmark directory (contains .aixcc/)
        project_dir: Path to project source repository
        output_path: Optional custom output path for test.sh
        litellm_base_url: Optional LiteLLM proxy URL
        litellm_api_key: Optional LiteLLM API key
        with_docker_testing: Enable iterative Docker testing via MCP (default: True)
        model: Model to use (default: claude-sonnet-4-5-20250929)
        verbose: Enable verbose logging

    Returns:
        Dictionary with generation results:
        {
            "success": bool,
            "test_sh_path": str,               # benchmarks/<name>/test.sh
            "bad_patch_path": str,             # benchmarks/<name>/bad_patch.diff
            "analysis_md_path": str,           # .agent/test_analysis.md
            "rationale_md_path": str,          # .agent/test_sh_rationale.md
            "agent_log_path": str,             # .agent/agent_log.txt
            "execution_log_path": str,         # .agent/test_sh_execution.log
            "test_sh_executed": bool,          # Whether test.sh ran successfully
            "message": str
        }
    """
    if not os.path.isdir(benchmark_dir):
        return {
            "success": False,
            "message": f"Benchmark directory not found: {benchmark_dir}"
        }

    if not os.path.isdir(project_dir):
        return {
            "success": False,
            "message": f"Project directory not found: {project_dir}"
        }

    # Check if test.sh already exists
    default_test_sh_path = os.path.join(benchmark_dir, "test.sh")
    if output_path is None:
        output_path = default_test_sh_path

    if os.path.exists(output_path) and not verbose:
        return {
            "success": False,
            "message": f"test.sh already exists at {output_path}. Use --force to overwrite."
        }

    # Create generator
    generator = ShTestGenerator(
        litellm_base_url=litellm_base_url,
        litellm_api_key=litellm_api_key,
        model=model
    )

    # Step 0: Copy benchmark OSS-Fuzz files to project directory
    if verbose:
        print(f"📋 Copying OSS-Fuzz files to {project_dir}/.oss-fuzz/...")

    _copy_benchmark_files_to_project(benchmark_dir, project_dir, verbose)

    # Step 1: Find unit tests
    if verbose:
        print(f"🔍 Analyzing unit tests in {project_dir}...")

    test_analysis_md, analysis_log = generator.find_unit_tests_sync(project_dir, benchmark_dir, verbose)

    # Save analysis markdown to .agent directory
    # Only write if agent didn't already create it via skill
    agent_dir = os.path.join(benchmark_dir, ".agent")
    os.makedirs(agent_dir, exist_ok=True)

    analysis_md_path = os.path.join(agent_dir, "test_analysis.md")
    if os.path.exists(analysis_md_path):
        if verbose:
            print(f"✅ Agent already created analysis at {analysis_md_path}")
    else:
        # Fallback: save extracted response if agent didn't create the file
        with open(analysis_md_path, "w") as f:
            f.write(test_analysis_md)
        if verbose:
            print(f"✅ Test analysis saved to {analysis_md_path}")

    # Step 2: Generate test.sh and bad_patch.diff (integrated in same agent)
    if verbose:
        mode_msg = "with Docker testing" if with_docker_testing else "two-phase"
        print(f"🔧 Generating test.sh and bad_patch.diff ({mode_msg})...")

    test_sh_content, agent_response_text, generation_log = generator.generate_test_sh_script_sync(
        test_analysis_md,
        benchmark_name,
        benchmark_dir,
        with_docker_testing,
        verbose
    )

    # Save agent response (rationale) to .agent/test_sh_rationale.md
    # Only write if agent didn't already create it via skill
    rationale_md_path = os.path.join(agent_dir, "test_sh_rationale.md")
    if os.path.exists(rationale_md_path):
        if verbose:
            print(f"✅ Agent already created rationale at {rationale_md_path}")
    else:
        # Fallback: save extracted response if agent didn't create the file
        with open(rationale_md_path, "w") as f:
            f.write(f"""# test.sh Generation Rationale

Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Method: {"iterative Docker testing (MCP-enhanced)" if with_docker_testing else "two-phase analysis"}

## Agent Response

{agent_response_text}
""")
        if verbose:
            print(f"✅ Agent response saved to {rationale_md_path}")

    # Verify bad_patch.diff was created by agent
    bad_patch_path = os.path.join(benchmark_dir, "bad_patch.diff")
    if not os.path.exists(bad_patch_path):
        if verbose:
            print(f"⚠️  Warning: bad_patch.diff not found at {bad_patch_path}")
            print(f"   Agent may have failed to generate bad_patch.diff")
    else:
        if verbose:
            print(f"✅ bad_patch.diff created by agent at {bad_patch_path}")

    # Save test.sh only if NOT using Docker testing
    # (Docker testing mode: agent already created test.sh using Write/Edit tools)
    if not with_docker_testing:
        # Add header comment to test.sh
        test_sh_content = _add_generation_header(
            test_sh_content,
            benchmark_name,
            with_docker_testing
        )

        # Save test.sh
        with open(output_path, "w") as f:
            f.write(test_sh_content)

        # Make executable
        os.chmod(output_path, 0o755)

        if verbose:
            print(f"✅ test.sh generated at {output_path}")
    else:
        # Docker testing mode: test.sh already exists (created by agent)
        if verbose:
            print(f"ℹ️  test.sh already created by agent during Docker testing at {output_path}")

        # Verify test.sh exists
        if not os.path.exists(output_path):
            if verbose:
                print(f"⚠️  Warning: test.sh not found at {output_path}, saving extracted script")
            # Fallback: save extracted script
            test_sh_content = _add_generation_header(
                test_sh_content,
                benchmark_name,
                with_docker_testing
            )
            with open(output_path, "w") as f:
                f.write(test_sh_content)
            os.chmod(output_path, 0o755)

    # Step 3: Save combined agent log to .agent directory
    agent_log_path = os.path.join(agent_dir, "agent_log.txt")

    # Delete existing agent_log.txt if it exists
    if os.path.exists(agent_log_path):
        os.remove(agent_log_path)
        if verbose:
            print(f"🗑️  Removed existing agent_log.txt")

    # Determine generation method
    method_description = (
        "Phase 1: Unit test discovery using Claude Agent SDK\n"
        "Phase 2: Test.sh and bad_patch.diff generation (integrated) "
    )
    if with_docker_testing:
        method_description += "with MCP-enhanced Docker testing (iterative refinement)"
    else:
        method_description += "using two-phase analysis (no Docker testing)"

    combined_log = f"""# Test.sh and Bad Patch Generation Agent Log
Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Project Directory: {project_dir}
Method: {method_description}

{analysis_log}
{generation_log}
"""
    with open(agent_log_path, "w") as f:
        f.write(combined_log)

    if verbose:
        print(f"✅ Agent log saved to {agent_log_path}")

    # Step 4: Execute test.sh and save output
    if verbose:
        print(f"🧪 Executing test.sh to verify functionality...")

    execution_log_path = os.path.join(agent_dir, "test_sh_execution.log")
    execution_success = False
    execution_output = ""

    try:
        # Import and call check_test_sh from MCP server
        from crsbench.migration.crsbench_mcp_server import check_test_sh
        import asyncio

        # Run check_test_sh asynchronously
        execution_output = asyncio.run(check_test_sh(benchmark_name))
        execution_success = "Error:" not in execution_output

        # Save execution output
        with open(execution_log_path, "w") as f:
            f.write(f"""# test.sh Execution Log

Executed: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Status: {"✅ Success" if execution_success else "❌ Failed"}

## Output

{execution_output}
""")

        if verbose:
            if execution_success:
                print(f"✅ test.sh executed successfully")
            else:
                print(f"⚠️  test.sh execution had issues (see {execution_log_path})")
            print(f"📄 Execution log saved to {execution_log_path}")

    except Exception as e:
        execution_output = f"Error executing test.sh: {str(e)}"
        execution_success = False

        # Save error log
        with open(execution_log_path, "w") as f:
            f.write(f"""# test.sh Execution Log

Executed: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Status: ❌ Execution Failed

## Error

{execution_output}
""")

        if verbose:
            print(f"❌ Failed to execute test.sh: {e}")
            print(f"📄 Error log saved to {execution_log_path}")

    return {
        "success": True,
        "test_sh_path": output_path,
        "bad_patch_path": bad_patch_path,
        "analysis_md_path": analysis_md_path,
        "rationale_md_path": rationale_md_path,
        "agent_log_path": agent_log_path,
        "execution_log_path": execution_log_path,
        "test_sh_executed": execution_success,
        "message": f"Successfully generated test.sh and bad_patch.diff for {benchmark_name}"
    }
