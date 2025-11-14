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

from claude_agent_sdk import query, ClaudeAgentOptions, PermissionResultAllow, ToolPermissionContext


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
        model: str = "claude-sonnet-4-5-20250929",
        auto_approve_bash: bool = True
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
        self.auto_approve_bash = auto_approve_bash

    async def _permission_callback(self, permission_context: ToolPermissionContext):
        """
        Auto-approve all tool operations for test.sh generation.

        This is safe because:
        - We're only analyzing code (Read, Grep, Glob)
        - Bash commands are for file exploration (ls, find, etc.)
        - Build/test execution happens in Docker via MCP tools
        """
        tool_name = permission_context.tool_name

        # Auto-approve all operations for test.sh generation
        if self.auto_approve_bash:
            return PermissionResultAllow()

        # Default: allow
        return PermissionResultAllow()

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
        verbose: bool = False
    ) -> tuple[str, str]:
        """
        Find unit tests in the project repository.

        Args:
            project_dir: Directory containing the project source code
            verbose: Enable verbose logging

        Returns:
            Tuple of (markdown_document, agent_log)
        """
        prompt = f"""You are an expert software testing analyst specialized in OSS-Fuzz projects.

Your task is to analyze the project repository at `{project_dir}` and identify all unit tests and functional tests.

# OSS-Fuzz Context
This is an OSS-Fuzz project. The `.oss-fuzz/` directory in the project root contains:
- **build.sh**: Build process used in OSS-Fuzz container
- **Dockerfile**: Dependencies and build environment setup
- **.aixcc/meta.yaml**: Harness file information and POV metadata

You must examine these files to understand how the project is built and tested in the OSS-Fuzz environment.

# Your Task
1. **Identify the build system**: Check for pom.xml (Maven), build.gradle (Gradle), CMakeLists.txt (CMake), Makefile, setup.py, etc.
2. **Find test files**: Use Glob to search for test directories (test/, tests/, src/test/) and test files (*Test.java, *_test.py, test_*.cpp, etc.)
3. **Identify test framework**: Determine which test framework is used (JUnit, pytest, Google Test, CTest, etc.)
4. **Extract test commands**: Document the exact commands needed to build and run tests
5. **Identify problematic tests**: Find tests that may fail in Docker (file permissions, network, root-only tests, flaky tests)

# Output Format
Provide a markdown document with this structure:

```markdown
# Unit Test Analysis

## Build System
- Type: [Maven/Gradle/CMake/Make/pytest/etc.]
- Language: [Java/C/C++/Python/Go/etc.]
- Build file: [path/to/build/file]

## Test Framework
- Framework: [JUnit 5/pytest/Google Test/etc.]
- Version: [if detectable]

## Test Commands
### Build Command
```bash
[exact command to build, e.g., mvn compile]
```

### Test Execution Command
```bash
[exact command to run tests, e.g., mvn test]
```

## Discovered Tests
[List 5-10 representative test files with paths]

## Test Exclusions
- Docker-incompatible tests: [list with reasons]
- Flaky tests: [list if any]
- Skip flags needed: [e.g., -Drat.skip=true -Dcheckstyle.skip=true]

## Recommendations for test.sh
- **Prefer simple, existing test commands** - if the project has a simple command like `make check`, `mvn test`, or `pytest`, use that instead of running individual tests
- Final test command with all necessary flags
- Environment variables needed (e.g., MVN, PYTHON)
- Working directory requirements

## Patch Exclude List
**IMPORTANT**: This section identifies files that should NOT receive patches from automated repair systems (CRS).

Analyze the project structure and identify files that should be excluded from patching:

1. **Test files** - Files in test directories (test/, tests/, src/test/, *Test.java, *_test.py, test_*.cpp, etc.)
   - Reason: Test files verify behavior and should not be modified by repair systems
   - List specific patterns or directories

2. **Build/Configuration files** - Build scripts, configuration files, metadata
   - Examples: pom.xml, build.gradle, CMakeLists.txt, Makefile, setup.py, package.json
   - Reason: Build configuration should remain stable
   - List specific files found in the project

3. **Documentation and resources** - Markdown, text files, images, data files
   - Examples: README.md, *.txt, *.md, docs/, resources/, assets/
   - Reason: Non-code files should not be patched
   - List relevant patterns

4. **Generated files** - Auto-generated code, compiled outputs
   - Examples: target/, build/, dist/, *.class, *.pyc, node_modules/
   - Reason: Generated files should not be directly modified
   - List if detectable

5. **Third-party code** - Vendored dependencies, external libraries
   - Examples: vendor/, third_party/, external/, lib/
   - Reason: External code should not be patched
   - List if found

Provide the patch exclude list in this format:
```yaml
patch_exclude_list:
  - "test/**"           # All test files
  - "tests/**"          # Test directory
  - "src/test/**"       # Test source files
  - "pom.xml"           # Maven build file
  - "*.md"              # Documentation
  # Add project-specific patterns
```
```

# Important
- Use Grep and Glob extensively to discover build files and test files
- Read `.oss-fuzz/build.sh` and `.oss-fuzz/Dockerfile` first for context
- Focus on information needed to generate a working test.sh script
- Cite specific file paths when listing tests
- **CRITICAL**: test.sh is executed from $SRC directory via `bash /src/test.sh`
- The project source code is mounted at `/src/<project-name>` (read-write access)
- WORKDIR in Dockerfile is typically set to `$SRC/<project-name>` or `$SRC`
- **You can use Bash for file exploration** - use SIMPLE commands only:
  - File listing: ls, find, tree
  - File inspection: file, cat, head, tail, wc
  - Text search: grep (simple patterns only)
  - AVOID: Complex pipes, redirects, chained commands with && or ||
- **CRITICAL: DO NOT EXECUTE build or test commands** (mvn compile, mvn test, make, pytest, etc.)
- This is STATIC ANALYSIS - discover test files, build systems, and frameworks by examining code structure
- **CRITICAL**: Carefully analyze the project structure to create a comprehensive patch_exclude_list

Now analyze the project and provide the markdown document.
"""

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "TodoWrite"],
            permission_callback=self._permission_callback,
            system_prompt=(
                "You are a thorough software testing analyst. "
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
                                result_text += block.text
                                if verbose:
                                    # Print first 200 chars of agent response
                                    preview = block.text[:200]
                                    if len(block.text) > 200:
                                        preview += "..."
                                    print(f"💬 [Agent Response]\n{preview}\n")

                    elif isinstance(message.content, str):
                        result_text += message.content
                        if verbose:
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
    ) -> tuple[str, str]:
        """
        Generate test.sh script from test analysis markdown.

        Args:
            test_analysis_md: Markdown document from find_unit_tests()
            benchmark_name: Name of the benchmark
            benchmark_dir: Path to benchmark directory
            with_docker_testing: Enable iterative Docker testing and refinement
            verbose: Enable verbose logging

        Returns:
            Tuple of (script_content, agent_log)
        """
        if with_docker_testing:
            # Iterative approach with Docker testing
            prompt = f"""You are an expert software testing engineer specialized in creating test.sh scripts for CRSBench benchmarks.

CRSBench is a benchmark framework for evaluating Cyber Reasoning Systems (CRS). Each benchmark consists of:
- A Dockerfile for building the project
- A build.sh script for compiling the project
- A test.sh script for running functional/unit tests
- A project.yaml file with metadata

**CRITICAL REQUIREMENTS - READ CAREFULLY:**

Your task is to generate a WORKING, EXECUTABLE test.sh bash script for benchmark `{benchmark_name}`.

1. **OUTPUT FORMAT**: You MUST output a valid bash script, NOT markdown, NOT explanations
2. **SCRIPT MUST BE EXECUTABLE**: The script must run successfully inside Docker container
3. **DO NOT FINISH UNTIL SUCCESS**: You MUST keep iterating until the test.sh runs without errors in Docker
4. **NO MARKDOWN**: Do NOT output markdown documents - only executable bash script

**What the test.sh script should do:**
1. Run the project's unit/functional tests (NOT fuzzers - fuzzers are separate from unit tests)
2. Skip non-essential checks (coverage, linting, style)
3. Exclude tests that fail in Docker (permissions, network)
4. Exit with 0 on success, non-zero on failure

**IMPORTANT**:
- Fuzzer executables (like *_fuzzer, *_harness) are NOT unit tests
- Only run the project's native unit/functional test suite (e.g., JUnit tests, pytest tests, make test)
- Do NOT execute fuzzer binaries built by OSS-Fuzz

# Test Analysis
The following analysis was performed on the project:

```markdown
{test_analysis_md}
```

# Available MCP Tools (Docker Operations)
- **mcp__crsbench__build_benchmark**: Build Docker image
- **mcp__crsbench__check_test_sh**: Test test.sh inside Docker container
- **mcp__crsbench__run_command_in_container**: Run commands inside Docker (e.g., 'mvn --version', 'which make')
- **mcp__crsbench__get_build_logs**: Get Docker build logs
- **mcp__crsbench__get_benchmark_info**: Get benchmark metadata

# Available File/Research Tools
- **Read**: Read file contents (project.yaml, Dockerfile, build.sh)
- **Write/Edit**: Create or modify test.sh scripts
- **Grep/Glob**: Search for files and patterns
- **WebSearch/WebFetch**: Research build systems and frameworks

**CRITICAL - BUILD/TEST EXECUTION ENVIRONMENT:**
- **Code analysis** (Read, Grep, Glob, Bash for ls/find): Run on HOST (current environment)
- **Build execution** (mvn compile, make, cmake, etc.): Run in DOCKER via mcp__crsbench__build_benchmark
- **Test execution** (mvn test, pytest, make test, etc.): Run in DOCKER via mcp__crsbench__check_test_sh
- **Tool availability checks**: Run in DOCKER via mcp__crsbench__run_command_in_container
- DO NOT use Bash to execute build/test commands locally - use MCP tools to run them in Docker

# Iterative Process - MANDATORY WORKFLOW
Follow these steps and DO NOT STOP until test.sh succeeds:

1. **Examine benchmark files**: Read .oss-fuzz/project.yaml, .oss-fuzz/Dockerfile, .oss-fuzz/build.sh
2. **Generate initial test.sh**: Create a BASH SCRIPT (NOT markdown) based on the test analysis
3. **Build Docker image**: Use mcp__crsbench__build_benchmark
4. **Test the script**: Use mcp__crsbench__check_test_sh to run test.sh in Docker
5. **Check result**:
   - ✅ If "test.sh execution succeeded" → YOU ARE DONE, output the final script
   - ❌ If failed → GO TO STEP 6
6. **Analyze failures**: Examine the logs carefully to understand why it failed
7. **Refine script**: Update test.sh with better skip flags, exclusions, or commands
8. **MANDATORY ITERATION**: GO BACK TO STEP 4 and test again
9. **REPEAT UNTIL SUCCESS**: You MUST keep iterating until test.sh runs successfully

**YOU MUST NOT FINISH THIS TASK UNTIL test.sh EXECUTES SUCCESSFULLY IN DOCKER**

The test.sh script runs inside a Docker container with:
- WORKDIR typically set to $SRC/<project-name> (or $SRC if not specified)
- **CRITICAL: test.sh is executed from $SRC directory** (not from project directory)
- Project source code is mounted at `/src/<project-name>` (read-write)
- Standard OSS-Fuzz environment variables available
- May be running as root

# Script Requirements
The test.sh must:
- Start with `#!/bin/bash`
- **Prefer simple, existing test commands** - if the project has `make check`, `mvn test`, or similar, use that instead of running individual test executables
- **CRITICAL: Skip tests that fail when run in the original project** - check the "Test Exclusions" section in the test analysis
- Use environment variables with fallbacks (e.g., `if [ -z "${{MVN}}" ]; then MVN=mvn; fi`)
- Skip non-essential checks (use flags like `-Drat.skip=true -Dcheckstyle.skip=true`)
- Exclude problematic tests (use patterns like `-Dtest=!FlakyTest,!NetworkTest`)
- Exit with appropriate status code

# Example Patterns

**Simple Make (PREFERRED if available):**
```bash
#!/bin/bash
make check
```

**Maven:**
```bash
#!/bin/bash
MAVEN_ARGS="-Djacoco.skip=true -Drat.skip=true -Dcheckstyle.skip=true -Dtest=!FlakyTest"
if [ -z "${{MVN}}" ]; then MVN=mvn; fi
$MVN test $MAVEN_ARGS
```

**Make/CMake:**
```bash
#!/bin/bash
mkdir -p build && cd build
cmake .. -DBUILD_TESTING=ON
make test
```

**Python:**
```bash
#!/bin/bash
if [ -z "${{PYTHON}}" ]; then PYTHON=python3; fi
$PYTHON -m pytest tests/ -k "not slow and not network"
```

**MANDATORY WORKFLOW - DO NOT SKIP:**

1. Start by generating a bash script (NOT markdown)
2. Use mcp__crsbench__build_benchmark to build Docker image (DO NOT use local docker commands)
3. Use mcp__crsbench__check_test_sh to test the script inside Docker
4. If test fails, analyze logs and refine the script using Edit tool
5. Test again with mcp__crsbench__check_test_sh (inside Docker)
6. REPEAT steps 4-5 until test.sh succeeds

**CRITICAL RULES:**
- Output ONLY executable bash script, NO markdown, NO explanations in the final output
- DO NOT finish until mcp__crsbench__check_test_sh returns "test.sh execution succeeded"
- If test.sh fails, you MUST keep iterating and fixing it
- **EXECUTION ENVIRONMENT RULES:**
  - Static analysis (Read, Grep, Glob, Bash ls/find/cat): Run on HOST
  - Build execution (mvn compile, make, etc.): Run in DOCKER via mcp__crsbench__build_benchmark
  - Test execution (mvn test, pytest, etc.): Run in DOCKER via mcp__crsbench__check_test_sh
  - DO NOT use Bash to run build/test commands - use MCP tools to execute them in Docker
- If you need to check tool availability in Docker, use mcp__crsbench__run_command_in_container
- TIP: Simpler is better - prefer `make check` or `mvn test` over individual test executables

Begin the iterative process now and DO NOT STOP until test.sh works successfully in Docker.
"""
        else:
            # Simple two-phase approach (analyze → generate)
            prompt = f"""You are an expert bash scripting engineer specialized in OSS-Fuzz test scripts.

You are tasked with generating a test.sh script for the OSS-Fuzz benchmark `{benchmark_name}`.

# Test Analysis
The following analysis was performed on the project:

```markdown
{test_analysis_md}
```

# OSS-Fuzz Context
The test.sh script will run inside an OSS-Fuzz Docker container after the project is built.

**CRITICAL Execution Environment:**
- **test.sh is executed from $SRC directory** via `bash /src/test.sh`
- The project source code is mounted at `/src/<project-name>` (read-write access)
- WORKDIR in Dockerfile is typically `$SRC/<project-name>` or `$SRC`
- You may need to `cd` into the project directory if tests expect to run from project root

# Requirements
You must create a test.sh script that:
1. Starts with `#!/bin/bash`
2. Runs the project's unit/functional tests (NOT fuzzers - fuzzers are separate from unit tests)
3. **Prefer simple, existing test commands** - if the project has `make check`, `mvn test`, or similar, use that instead of running individual test executables
4. **CRITICAL: Skip tests that fail when run in the original project** - check the "Test Exclusions" section in the analysis
5. Excludes tests that fail in Docker (file permissions, network, root-only, flaky tests)
6. Skips non-essential checks (coverage, linting, code style, RAT checks)
7. Exits with 0 on success, non-zero on failure
8. Uses environment variables for commands (e.g., `${{MVN}}`, `${{PYTHON}}`) with fallbacks

**IMPORTANT**:
- Fuzzer executables (like *_fuzzer, *_harness) are NOT unit tests
- Only run the project's native unit/functional test suite (e.g., JUnit tests, pytest tests, make test)
- Do NOT execute fuzzer binaries built by OSS-Fuzz

# Example Patterns

**Simple Make (PREFERRED if available):**
```bash
#!/bin/bash
make check
```

**Maven:**
```bash
#!/bin/bash
MAVEN_ARGS="-Djacoco.skip=true -Drat.skip=true -Dcheckstyle.skip=true -Dtest=!FlakyTest"
if [ -z "${{MVN}}" ]; then MVN=mvn; fi
$MVN test $MAVEN_ARGS
```

**Make/CMake:**
```bash
#!/bin/bash
mkdir -p build && cd build
cmake .. -DBUILD_TESTING=ON
make test
```

**Python:**
```bash
#!/bin/bash
if [ -z "${{PYTHON}}" ]; then PYTHON=python3; fi
$PYTHON -m pytest tests/ -k "not slow"
```

# Your Task
Based on the test analysis above:
1. Determine the build system and test framework
2. Generate the appropriate test.sh script following the patterns
3. Include all necessary skip flags from the "Test Exclusions" section
4. Ensure the script will work correctly from the WORKDIR

# Output Format
Provide ONLY the bash script content. No explanations, no markdown code fences, just the raw script starting with #!/bin/bash.
**TIP: Simpler is better** - if the project already has a convenient test command (like `make check`), prefer that over running individual test executables.

Generate the test.sh script now:
"""

        # Configure tools based on mode
        if with_docker_testing:
            from pathlib import Path
            mcp_server_script = Path(__file__).parent / "crsbench_mcp_server.py"

            allowed_tools = [
                "Read", "Write", "Edit", "Grep", "Glob",
                "WebSearch", "WebFetch", "TodoWrite",
                # MCP tools for Docker operations
                "mcp__crsbench__build_benchmark",
                "mcp__crsbench__get_build_logs",
                "mcp__crsbench__check_test_sh",
                "mcp__crsbench__check_build_sh",
                "mcp__crsbench__run_command_in_container",
                "mcp__crsbench__get_benchmark_info"
            ]

            mcp_servers = {
                "crsbench": {
                    "command": "python3",
                    "args": [str(mcp_server_script), benchmark_name]
                }
            }

            system_prompt = (
                "You are an expert test.sh script generator with access to Docker build and test tools. "
                "\n\n"
                "**CRITICAL OUTPUT REQUIREMENT:**\n"
                "- Your final output MUST be an EXECUTABLE BASH SCRIPT\n"
                "- DO NOT output markdown, explanations, or analysis documents\n"
                "- The script must run successfully inside Docker container\n"
                "- DO NOT finish until the script executes successfully\n"
                "\n"
                "**CRITICAL EXECUTION ENVIRONMENT:**\n"
                "- **Static analysis** (Read, Grep, Glob, Bash for file exploration): Run on HOST\n"
                "- **Build execution** (mvn compile, make, cmake, etc.): MUST run in DOCKER container\n"
                "- **Test execution** (mvn test, pytest, make test, etc.): MUST run in DOCKER container\n"
                "- You MUST use MCP tools (mcp__crsbench__*) for ALL build/test execution\n"
                "- DO NOT use Bash tool to execute build/test commands - use MCP tools for that\n"
                "- Bash is OK for file exploration (ls, find, cat) but NOT for executing builds/tests\n"
                "- Use mcp__crsbench__* tools exclusively for Docker operations\n"
                "\n\n"
                "Available MCP tools:\n"
                "- mcp__crsbench__build_benchmark: Build Docker image for benchmark\n"
                "- mcp__crsbench__check_test_sh: Test the test.sh script in Docker container\n"
                "- mcp__crsbench__check_build_sh: Test the build.sh script in Docker container\n"
                "- mcp__crsbench__run_command_in_container: Run arbitrary commands inside Docker container (e.g., 'which sbt', 'mvn --version')\n"
                "- mcp__crsbench__get_build_logs: Get Docker build logs if build fails\n"
                "- mcp__crsbench__get_benchmark_info: Get benchmark metadata\n"
                "\n"
                "For file operations, use Read/Write/Edit/Grep/Glob. "
                "Use Write to create new test.sh scripts, Edit to modify existing scripts. "
                "For research, use WebSearch/WebFetch. "
                "Use TodoWrite to track your iterative refinement process. "
                "\n\n"
                "Tool Availability Check:\n"
                "Use mcp__crsbench__run_command_in_container to check what tools are available inside the Docker container. "
                "For example: run_command_in_container(benchmark_name, 'which sbt') to check if SBT is installed. "
                "This is MORE ACCURATE than reading Dockerfile because it checks the actual container environment.\n"
                "\n\n"
                "**MANDATORY WORKFLOW - DO NOT DEVIATE:**\n"
                "1. Use mcp__crsbench__get_benchmark_info to understand the benchmark\n"
                "2. Generate test.sh bash script (NOT markdown) based on analysis\n"
                "3. Use mcp__crsbench__build_benchmark to build Docker image (DO NOT use Bash for docker build)\n"
                "4. Use mcp__crsbench__run_command_in_container to verify tool availability if needed (inside Docker)\n"
                "5. Use mcp__crsbench__check_test_sh to test the script (inside Docker)\n"
                "6. Check the result:\n"
                "   - If 'test.sh execution succeeded' → YOU ARE DONE\n"
                "   - If failed → Analyze logs, use Edit to fix the script, GO BACK TO STEP 5\n"
                "7. KEEP ITERATING until test.sh succeeds - DO NOT give up or finish early\n"
                "\n"
                "**EXECUTION ENVIRONMENT RULES:**\n"
                "- Static analysis (Read/Grep/Glob/Bash for ls/find): HOST (current environment)\n"
                "- Build operations (mvn compile, make, cmake): DOCKER via mcp__crsbench__build_benchmark\n"
                "- Test operations (mvn test, pytest, make test): DOCKER via mcp__crsbench__check_test_sh\n"
                "- Tool availability checks (which mvn, sbt --version): DOCKER via mcp__crsbench__run_command_in_container\n"
                "- DO NOT execute build/test commands with Bash - analyze code structure only on host\n"
                "\n"
                "**YOU MUST NOT FINISH THIS TASK UNTIL test.sh EXECUTES SUCCESSFULLY IN DOCKER**"
            )
        else:
            allowed_tools = ["Read", "Write", "Edit", "WebSearch", "WebFetch", "TodoWrite"]
            mcp_servers = None
            system_prompt = (
                "You are a bash scripting expert. "
                "Generate clean, working bash scripts following the patterns provided. "
                "You can use Read to examine files, Write to create files, Edit to modify files, "
                "WebSearch to research build system patterns, WebFetch for official docs, "
                "and TodoWrite to organize your work. "
                "Output only the script content, no extra text."
            )

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            permission_callback=self._permission_callback,
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
                                script_content += block.text
                                if verbose:
                                    # Print first 200 chars of agent response
                                    preview = block.text[:200]
                                    if len(block.text) > 200:
                                        preview += "..."
                                    print(f"💬 [Agent Response]\n{preview}\n")

                    elif isinstance(message.content, str):
                        script_content += message.content
                        if verbose:
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

        # Clean up the script content
        script_content = script_content.strip()

        # Extract actual bash script from agent response
        # Agent may include explanations, so we need to extract the script
        extracted_script = self._extract_bash_script(script_content)
        if extracted_script:
            script_content = extracted_script

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

        return script_content, agent_log

    def find_unit_tests_sync(
        self,
        project_dir: str,
        verbose: bool = False
    ) -> tuple[str, str]:
        """Synchronous wrapper for find_unit_tests.

        Returns:
            Tuple of (markdown_document, agent_log)
        """
        return asyncio.run(self.find_unit_tests(project_dir, verbose))

    def generate_test_sh_script_sync(
        self,
        test_analysis_md: str,
        benchmark_name: str,
        benchmark_dir: str,
        with_docker_testing: bool = False,
        verbose: bool = False
    ) -> tuple[str, str]:
        """Synchronous wrapper for generate_test_sh_script.

        Returns:
            Tuple of (script_content, agent_log)
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
    Generate test.sh for a benchmark.

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
            "test_sh_path": str,
            "analysis_md_path": str,
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

    test_analysis_md, analysis_log = generator.find_unit_tests_sync(project_dir, verbose)

    # Save analysis markdown
    analysis_md_path = os.path.join(benchmark_dir, ".aixcc", "test_analysis.md")
    os.makedirs(os.path.dirname(analysis_md_path), exist_ok=True)
    with open(analysis_md_path, "w") as f:
        f.write(test_analysis_md)

    if verbose:
        print(f"✅ Test analysis saved to {analysis_md_path}")

    # Step 2: Generate test.sh
    if verbose:
        mode_msg = "with Docker testing" if with_docker_testing else "two-phase"
        print(f"🔧 Generating test.sh script ({mode_msg})...")

    test_sh_content, generation_log = generator.generate_test_sh_script_sync(
        test_analysis_md,
        benchmark_name,
        benchmark_dir,
        with_docker_testing,
        verbose
    )

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

    # Step 3: Save combined agent log
    agent_log_path = os.path.join(benchmark_dir, ".aixcc", "agent_log.txt")

    # Delete existing agent_log.txt if it exists
    if os.path.exists(agent_log_path):
        os.remove(agent_log_path)
        if verbose:
            print(f"🗑️  Removed existing agent_log.txt")

    # Determine generation method
    method_description = (
        "Phase 1: Unit test discovery using Claude Agent SDK\n"
        "Phase 2: Test.sh generation "
    )
    if with_docker_testing:
        method_description += "with MCP-enhanced Docker testing (iterative refinement)"
    else:
        method_description += "using two-phase analysis (no Docker testing)"

    combined_log = f"""# Test.sh Generation Agent Log
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

    return {
        "success": True,
        "test_sh_path": output_path,
        "analysis_md_path": analysis_md_path,
        "agent_log_path": agent_log_path,
        "message": f"Successfully generated test.sh for {benchmark_name}"
    }
