"""
ShTestGenerator class for test.sh generation using Claude Agent SDK.

This module contains the main generator class that uses Claude Agent SDK
to analyze projects and generate test.sh scripts.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, cast

from claude_agent_sdk import ClaudeAgentOptions, query

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _get_crsbench_repo_root() -> str:
    """Get the crsbench repository root directory.

    Returns:
        Absolute path to crsbench repository root
    """
    from crsbench.utils.paths import get_crsbench_root

    return str(get_crsbench_root())


class ShTestGenerator:
    """
    Agent-based test.sh generator using Claude Agent SDK.

    Uses LiteLLM proxy for Claude API access via CRSBENCH_LLM_BASE_URL
    and CRSBENCH_LLM_API_KEY.
    """

    def __init__(
        self,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929",
    ):
        """
        Initialize the test.sh generator agent.

        Args:
            litellm_base_url: LiteLLM proxy URL
                (defaults to CRSBENCH_LLM_BASE_URL env var)
            litellm_api_key: LiteLLM API key
                (defaults to CRSBENCH_LLM_API_KEY env var)
            model: Model to use via LiteLLM
        """
        self.litellm_base_url = litellm_base_url or os.getenv("CRSBENCH_LLM_BASE_URL")
        self.litellm_api_key = litellm_api_key or os.getenv("CRSBENCH_LLM_API_KEY")

        if not self.litellm_base_url:
            raise ValueError(
                "CRSBENCH_LLM_BASE_URL must be set in environment or passed as parameter"
            )
        if not self.litellm_api_key:
            raise ValueError(
                "CRSBENCH_LLM_API_KEY must be set in environment or passed as parameter"
            )

        self.model = model

    def _get_env_dict(self) -> dict[str, str]:
        """Return env dict for Claude Agent SDK with validated credentials."""
        # These are validated in __init__ to be non-None
        assert self.litellm_base_url is not None
        assert self.litellm_api_key is not None
        return {
            "ANTHROPIC_BASE_URL": self.litellm_base_url,
            "ANTHROPIC_AUTH_TOKEN": self.litellm_api_key,
        }

    def _extract_bash_script(self, text: str) -> Optional[str]:
        """
        Extract the actual bash script from agent response text.

        Agent may include explanations and markdown. This extracts:
        1. Content between ```bash and ``` markers
        2. Or content starting from #!/bin/bash to end of script

        Returns:
            Extracted bash script, or None if not found
        """
        # Try to find bash code block in markdown
        bash_block_pattern = r"```bash\s*\n(.*?)\n```"
        matches = re.findall(bash_block_pattern, text, re.DOTALL)
        if matches:
            # Return the last (most recent) bash block
            return matches[-1].strip()

        # Try to find content starting with #!/bin/bash
        shebang_pattern = r"(#!/bin/bash.*?)(?=\n\n[#]{2,}|\n\n[A-Z][a-z]+:|\Z)"
        matches = re.findall(shebang_pattern, text, re.DOTALL)
        if matches:
            # Return the longest match (most likely the actual script)
            longest_match = cast("str", max(matches, key=len))
            return longest_match.strip()

        return None

    def _format_agent_log(self, messages: List[Any], phase_name: str) -> str:
        """
        Format agent messages into readable log.

        Args:
            messages: List of agent messages
            phase_name: Name of the phase (e.g., "Unit Test Discovery")

        Returns:
            Formatted log string
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_lines = [f"\n{'=' * 70}"]
        log_lines.append(f"Phase: {phase_name}")
        log_lines.append(f"Time: {timestamp}")
        log_lines.append(f"{'=' * 70}\n")

        for idx, message in enumerate(messages):
            # Add message header with role
            role = getattr(message, "role", "unknown")
            log_lines.append(f"\n--- Message {idx + 1} ({role}) ---")

            if hasattr(message, "content") and message.content:
                if isinstance(message.content, list):
                    for block in message.content:
                        block_type = type(block).__name__

                        # Tool use block (assistant calls a tool)
                        if block_type == "ToolUseBlock":
                            tool_id = getattr(block, "id", "unknown")
                            tool_name = getattr(block, "name", "unknown")
                            log_lines.append(f"[Tool Call] {tool_name} (id: {tool_id})")
                            if hasattr(block, "input") and block.input:
                                # Format input nicely
                                input_str = (
                                    json.dumps(block.input, indent=2)
                                    if isinstance(block.input, dict)
                                    else str(block.input)
                                )
                                log_lines.append(f"  Input: {input_str}")

                        # Tool result block (user/system returns tool result)
                        elif block_type == "ToolResultBlock":
                            tool_id = getattr(block, "tool_use_id", "unknown")
                            log_lines.append(f"[Tool Result] (id: {tool_id})")
                            if hasattr(block, "content"):
                                content = str(block.content)[:500]  # Limit to 500 chars
                                if len(str(block.content)) > 500:
                                    content += "... (truncated)"
                                log_lines.append(f"  Result: {content}")

                        # Text block (agent thinking/response)
                        elif hasattr(block, "text"):
                            log_lines.append(f"[Text]\n{block.text}\n")

                elif isinstance(message.content, str):
                    log_lines.append(f"[Text]\n{message.content}\n")

        log_lines.append("")
        return "\n".join(log_lines)

    async def find_unit_tests(
        self, project_dir: str, benchmark_dir: str, *, verbose: bool = False
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
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                "Bash",
                "WebSearch",
                "WebFetch",
                "TodoWrite",
                "Skill",
            ],
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
            env=self._get_env_dict(),
        )

        result_text = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    role = getattr(message, "role", "unknown")
                    logger.debug(f"Message (role: {role})")

                    if hasattr(message, "content") and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                block_type = type(block).__name__

                                if block_type == "ToolUseBlock":
                                    tool_id = getattr(block, "id", "unknown")
                                    tool_name = getattr(block, "name", "unknown")
                                    logger.debug(
                                        f"[Tool Call] {tool_name} (id: {tool_id})"
                                    )

                                elif block_type == "ToolResultBlock":
                                    tool_id = getattr(block, "tool_use_id", "unknown")
                                    logger.debug(f"[Tool Result] (id: {tool_id})")

                                elif hasattr(block, "text"):
                                    text: str = getattr(block, "text", "")
                                    preview = text[:200]
                                    if len(text) > 200:
                                        preview += "..."
                                    logger.debug(f"[Agent Response] {preview}")

                        elif isinstance(message.content, str):
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            logger.debug(f"[Agent Response] {preview}")

        except Exception as e:
            if verbose:
                logger.error(f"TestFinder agent error: {e}")
            result_text = f"# Error\n\nFailed to analyze project: {str(e)}"

        # Extract text from last assistant message only
        if not result_text:  # Only if no error occurred
            for message in reversed(messages):
                if getattr(message, "role", None) == "assistant":
                    if hasattr(message, "content") and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                if hasattr(block, "text"):
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
        *,
        with_docker_testing: bool = False,
        verbose: bool = False,
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
            prompt = self._build_docker_testing_prompt(
                test_analysis_md, benchmark_name, benchmark_dir
            )
        else:
            # Simple two-phase approach (analyze → generate)
            prompt = self._build_simple_prompt(
                test_analysis_md, benchmark_name, benchmark_dir
            )

        # Configure tools based on mode
        if with_docker_testing:
            mcp_server_script = Path(__file__).parent.parent / "mcp" / "server.py"

            allowed_tools = [
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                "Bash",
                "WebSearch",
                "WebFetch",
                "TodoWrite",
                "Skill",
                # MCP tools for Docker operations
                "mcp__crsbench__build_benchmark",
                "mcp__crsbench__check_test_sh",
                "mcp__crsbench__run_command_in_container",
                "mcp__crsbench__get_benchmark_info",
                "mcp__crsbench__verify_bad_patch",
            ]

            mcp_servers = {
                "crsbench": {
                    "command": "python3",
                    "args": [str(mcp_server_script), benchmark_name],
                }
            }

            system_prompt = self._build_docker_system_prompt()
        else:
            allowed_tools = [
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                "Bash",
                "WebSearch",
                "WebFetch",
                "TodoWrite",
                "Skill",
            ]
            mcp_servers = None
            system_prompt = self._build_simple_system_prompt()

        # Use repo root as cwd so skills can be loaded from .claude/skills/
        # The agent can still access benchmark files using absolute paths
        options_kwargs: dict[str, Any] = {
            "model": self.model,
            "allowed_tools": allowed_tools,
            "setting_sources": ["project"],
            "cwd": _get_crsbench_repo_root(),
            "system_prompt": system_prompt,
            "env": self._get_env_dict(),
        }
        if mcp_servers is not None:
            options_kwargs["mcp_servers"] = mcp_servers
        options = ClaudeAgentOptions(**options_kwargs)

        script_content = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    role = getattr(message, "role", "unknown")
                    logger.debug(f"Message (role: {role})")

                    if hasattr(message, "content") and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                block_type = type(block).__name__

                                if block_type == "ToolUseBlock":
                                    tool_id = getattr(block, "id", "unknown")
                                    tool_name = getattr(block, "name", "unknown")
                                    logger.debug(
                                        f"[Tool Call] {tool_name} (id: {tool_id})"
                                    )

                                elif block_type == "ToolResultBlock":
                                    tool_id = getattr(block, "tool_use_id", "unknown")
                                    logger.debug(f"[Tool Result] (id: {tool_id})")

                                elif hasattr(block, "text"):
                                    text: str = getattr(block, "text", "")
                                    preview = text[:200]
                                    if len(text) > 200:
                                        preview += "..."
                                    logger.debug(f"[Agent Response] {preview}")

                        elif isinstance(message.content, str):
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            logger.debug(f"[Agent Response] {preview}")

        except Exception as e:
            if verbose:
                logger.error(f"TestShGenerator agent error: {e}")
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
                if getattr(message, "role", None) == "assistant":
                    if hasattr(message, "content") and message.content:
                        if isinstance(message.content, list):
                            for block in message.content:
                                if hasattr(block, "text"):
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

    def _build_docker_testing_prompt(
        self, test_analysis_md: str, benchmark_name: str, benchmark_dir: str
    ) -> str:
        """Build prompt for Docker testing mode."""
        return f"""Generate BOTH test.sh and bad_patch.diff for benchmark `{benchmark_name}`.

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
- Assuming binaries exist in `/out/` - they DON'T
- Assuming `make test` will work without `make` first
- Assuming Maven test will work without compile first
- Skipping the build step entirely

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

    def _build_simple_prompt(
        self, test_analysis_md: str, benchmark_name: str, benchmark_dir: str
    ) -> str:
        """Build prompt for simple (non-Docker) mode."""
        return f"""Generate BOTH test.sh and bad_patch.diff for benchmark `{benchmark_name}`.

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
- Assuming binaries exist in `/out/` - they DON'T
- Assuming `make test` will work without `make` first
- Assuming Maven test will work without compile first
- Skipping the build step entirely

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

    def _build_docker_system_prompt(self) -> str:
        """Build system prompt for Docker testing mode."""
        return (
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

    def _build_simple_system_prompt(self) -> str:
        """Build system prompt for simple (non-Docker) mode."""
        return (
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

    def find_unit_tests_sync(
        self, project_dir: str, benchmark_dir: str, *, verbose: bool = False
    ) -> tuple[str, str]:
        """Synchronous wrapper for find_unit_tests.

        Returns:
            Tuple of (markdown_document, agent_log)
        """
        return asyncio.run(
            self.find_unit_tests(project_dir, benchmark_dir, verbose=verbose)
        )

    def generate_test_sh_script_sync(
        self,
        test_analysis_md: str,
        benchmark_name: str,
        benchmark_dir: str,
        *,
        with_docker_testing: bool = False,
        verbose: bool = False,
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
                with_docker_testing=with_docker_testing,
                verbose=verbose,
            )
        )

    async def generate_bad_patch(
        self,
        test_analysis_md: str,
        project_dir: str,
        benchmark_dir: str,
        *,
        verbose: bool = False,
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
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                "Bash",
                "WebSearch",
                "WebFetch",
                "TodoWrite",
                "Skill",
            ],
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
            env=self._get_env_dict(),
        )

        patch_content = ""
        messages = []
        try:
            async for message in query(prompt=prompt, options=options):
                messages.append(message)

                if verbose:
                    role = getattr(message, "role", "unknown")
                    logger.debug(f"Message (role: {role})")

                if hasattr(message, "content") and message.content:
                    if isinstance(message.content, list):
                        for block in message.content:
                            block_type = type(block).__name__

                            if block_type == "ToolUseBlock":
                                if verbose:
                                    tool_id = getattr(block, "id", "unknown")
                                    tool_name = getattr(block, "name", "unknown")
                                    logger.debug(
                                        f"[Tool Call] {tool_name} (id: {tool_id})"
                                    )

                            elif block_type == "ToolResultBlock":
                                if verbose:
                                    tool_id = getattr(block, "tool_use_id", "unknown")
                                    logger.debug(f"[Tool Result] (id: {tool_id})")

                            elif hasattr(block, "text"):
                                text: str = getattr(block, "text", "")
                                patch_content += text
                                if verbose:
                                    preview = text[:200]
                                    if len(text) > 200:
                                        preview += "..."
                                    logger.debug(f"[Agent Response] {preview}")

                    elif isinstance(message.content, str):
                        patch_content += message.content
                        if verbose:
                            preview = message.content[:200]
                            if len(message.content) > 200:
                                preview += "..."
                            logger.debug(f"[Agent Response] {preview}")

        except Exception as e:
            if verbose:
                logger.error(f"BadPatchGenerator agent error: {e}")
            patch_content = f"# Error generating bad patch: {str(e)}"

        # Save original agent response text
        agent_response_text = patch_content.strip()

        # Extract actual diff from agent response (may be in markdown code block)
        diff_block_pattern = r"```diff\s*\n(.*?)\n```"
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
        *,
        verbose: bool = False,
    ) -> tuple[str, str, str]:
        """Synchronous wrapper for generate_bad_patch.

        Returns:
            Tuple of (patch_content, agent_response_text, agent_log)
        """
        return asyncio.run(
            self.generate_bad_patch(
                test_analysis_md, project_dir, benchmark_dir, verbose=verbose
            )
        )
