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
        log_lines = [f"\n{'='*70}"]
        log_lines.append(f"Phase: {phase_name}")
        log_lines.append(f"{'='*70}\n")

        for message in messages:
            if hasattr(message, 'content') and message.content:
                if isinstance(message.content, list):
                    for block in message.content:
                        block_type = type(block).__name__

                        # Tool use block
                        if block_type == "ToolUseBlock":
                            log_lines.append(f"[Tool] {block.name}")
                            if hasattr(block, 'input') and block.input:
                                # Format input nicely
                                input_str = json.dumps(block.input, indent=2) if isinstance(block.input, dict) else str(block.input)
                                log_lines.append(f"  Input: {input_str}")

                        # Tool result block
                        elif block_type == "ToolResultBlock":
                            if hasattr(block, 'content'):
                                content = str(block.content)[:500]  # Limit to 500 chars
                                if len(str(block.content)) > 500:
                                    content += "... (truncated)"
                                log_lines.append(f"  Result: {content}")

                        # Text block (agent response)
                        elif hasattr(block, 'text'):
                            log_lines.append(f"\n[Agent Response]\n{block.text}\n")

                elif isinstance(message.content, str):
                    log_lines.append(f"\n[Agent Response]\n{message.content}\n")

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
        prompt = f"""You are a software testing expert analyzing a project repository.

# Task
Analyze the project repository at: `{project_dir}`

Find all unit tests and functional tests in this repository.

# OSS-Fuzz Context
This project is used in OSS-Fuzz for fuzzing. Check the `.oss-fuzz/` directory in the project root for:
- **build.sh**: Shows how the project is built in OSS-Fuzz container
- **Dockerfile**: Lists dependencies and build environment
- **.aixcc/meta.yaml**: Contains harness file information and POV metadata

These files provide helpful context about the build system and dependencies.

# Analysis Strategy

1. **Review OSS-Fuzz build configuration (if available)**
   - Check `.oss-fuzz/build.sh` to understand the build process
   - Check `.oss-fuzz/Dockerfile` for dependencies and environment setup
   - This helps identify the correct build commands and test execution method

2. **Identify build system**
   - Look for pom.xml (Maven), build.gradle (Gradle), CMakeLists.txt (CMake), Makefile, setup.py, etc.
   - Note the programming language (Java, C/C++, Python, etc.)

3. **Find test directories and files**
   - Search for common test directories: test/, tests/, src/test/, test/unit/
   - Find test files: *Test.java, *_test.py, test_*.py, *_test.cpp, etc.
   - Use Glob to list test files

4. **Identify test framework**
   - Maven/Java: JUnit (import org.junit), TestNG
   - Python: pytest, unittest
   - C/C++: Google Test, Catch2, CTest
   - Look for test framework imports/includes

5. **Analyze test structure**
   - Read a few representative test files
   - Note test class/function names
   - Identify test categories (unit, integration, functional)
   - Find any test exclusion patterns or skip annotations

6. **Document build and test commands**
   - How to build: mvn compile, make, cmake, etc.
   - How to run tests: mvn test, make test, pytest, etc.
   - Any special flags or configurations

# Output Format

Provide a markdown document with the following structure:

```markdown
# Unit Test Analysis for <project-name>

## Build System
- Type: Maven/Gradle/CMake/Make/etc.
- Language: Java/C/C++/Python/etc.
- Build file: path/to/pom.xml

## Test Framework
- Framework: JUnit 5/pytest/Google Test/etc.
- Version: (if detectable)

## Test Directory Structure
```
test/
├── unit/
│   ├── TestClass1.java
│   └── TestClass2.java
├── integration/
└── ...
```

## Discovered Tests
### Test File: src/test/java/com/example/FooTest.java
- Test Classes: FooTest
- Test Methods: testMethod1(), testMethod2()
- Dependencies: (if notable)

### Test File: src/test/java/com/example/BarTest.java
- Test Classes: BarTest
- Test Methods: testBar1(), testBar2()

(List 5-10 representative test files)

## Build Command
```
mvn compile
```

## Test Execution Command
```
mvn test
```

## Test Exclusions
- Tests that may fail in Docker: (list if any)
- Root-only tests: (list if any)
- Flaky tests: (list if any)

## Recommendations for test.sh
- Suggested command: mvn test -Dskip.coverage=true
- Flags to add: -Drat.skip=true, -Dcheckstyle.skip=true
- Tests to exclude: -Dtest=!FlakyTest,!RootOnlyTest
```

# Important
- Use Grep and Glob extensively to discover tests
- Read only representative test files (don't read all tests)
- Focus on information needed to generate test.sh
- Cite specific file paths when listing tests

Now analyze the project and provide the markdown document.
"""

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            system_prompt=(
                "You are a thorough software testing analyst. "
                "Use Grep to search patterns, Glob to find files, and Read to examine files. "
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
                    print(f"🔍 Message type: {type(message).__name__}")

                if hasattr(message, 'content') and message.content:
                    # Extract text content
                    if isinstance(message.content, list):
                        for block in message.content:
                            if hasattr(block, 'text'):
                                result_text += block.text
                    elif isinstance(message.content, str):
                        result_text += message.content

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
        verbose: bool = False
    ) -> tuple[str, str]:
        """
        Generate test.sh script from test analysis markdown.

        Args:
            test_analysis_md: Markdown document from find_unit_tests()
            benchmark_name: Name of the benchmark
            verbose: Enable verbose logging

        Returns:
            Tuple of (script_content, agent_log)
        """
        prompt = f"""You are a bash scripting expert creating test.sh for OSS-Fuzz benchmarks.

# Task
Generate a test.sh script for the benchmark: `{benchmark_name}`

Based on the following test analysis:

```markdown
{test_analysis_md}
```

# test.sh Requirements

The test.sh script:
1. Must be a bash script (#!/bin/bash)
2. Runs inside OSS-Fuzz Docker container (may be running as root)
3. Should run functional/unit tests to validate the project works correctly
4. Should exclude tests that fail in Docker environment (e.g., file permission tests)
5. Should skip non-essential checks (coverage, linting, style checks)
6. Should exit with 0 on success, non-zero on failure

# Example test.sh for Maven Projects

```bash
#!/bin/bash

# Maven test execution with common skips
MAVEN_ARGS="-Djacoco.skip=true -Drat.skip=true -Dcheckstyle.skip=true \\
  -Djavac.target.version=11 \\
  -Dtest=!FlakyTest,!RootRequiredTest"

if [ -z "${{MVN}}" ]; then
  MVN=mvn
fi

$MVN test $MAVEN_ARGS
```

# Example test.sh for Make Projects

```bash
#!/bin/bash

# Run test target
make test
```

# Example test.sh for CMake Projects

```bash
#!/bin/bash

# Build and run tests
mkdir -p build
cd build
cmake .. -DBUILD_TESTING=ON
make test
```

# Instructions

1. Determine build system from the test analysis
2. Generate appropriate test.sh following the patterns above
3. Include necessary test exclusions (based on "Test Exclusions" section)
4. Add common skip flags for the build system
5. Handle environment variables (like MVN, PYTHON, etc.)

# Output Format

Provide ONLY the test.sh script content. No explanation, no markdown fences, just the raw bash script.

Start with #!/bin/bash and end with the test command.

Generate the test.sh script now:
"""

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read"],  # Only reading for context
            system_prompt=(
                "You are a bash scripting expert. "
                "Generate clean, working bash scripts following the patterns provided. "
                "Output only the script content, no extra text."
            ),
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
                    print(f"🔍 Message type: {type(message).__name__}")

                if hasattr(message, 'content') and message.content:
                    # Extract text content
                    if isinstance(message.content, list):
                        for block in message.content:
                            if hasattr(block, 'text'):
                                script_content += block.text
                    elif isinstance(message.content, str):
                        script_content += message.content

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
        verbose: bool = False
    ) -> tuple[str, str]:
        """Synchronous wrapper for generate_test_sh_script.

        Returns:
            Tuple of (script_content, agent_log)
        """
        return asyncio.run(
            self.generate_test_sh_script(test_analysis_md, benchmark_name, verbose)
        )


def generate_test_sh_for_benchmark(
    benchmark_name: str,
    benchmark_dir: str,
    project_dir: str,
    output_path: Optional[str] = None,
    litellm_base_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
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
        litellm_api_key=litellm_api_key
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
        print(f"🔧 Generating test.sh script...")

    test_sh_content, generation_log = generator.generate_test_sh_script_sync(
        test_analysis_md, benchmark_name, verbose
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
    combined_log = f"""# Test.sh Generation Agent Log
Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Project Directory: {project_dir}

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
