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
        prompt = f"""Analyze the project repository at `{project_dir}` and identify all unit tests and functional tests.

This is an OSS-Fuzz project with `.oss-fuzz/` directory containing build.sh, Dockerfile, and .aixcc/meta.yaml.

Please use the appropriate skill to:
1. Identify the build system and test framework
2. Find all unit test files
3. Document test commands and exclusions
4. Generate a comprehensive patch exclude list

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
                "You are a thorough software testing analyst. "
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
            prompt = f"""Generate a WORKING, EXECUTABLE test.sh bash script for benchmark `{benchmark_name}`.

# Context
- Benchmark directory: {benchmark_dir}
- **CRITICAL**: build.sh and test.sh are located at $SRC directory in the container
- **CRITICAL**: test.sh is executed from $SRC directory via `bash $SRC/test.sh`
- Project source code is mounted at $SRC/<project-name> in the container

# Test Analysis
```markdown
{test_analysis_md}
```

# Your Task
Use the appropriate skill to:
1. Generate an initial test.sh script based on the analysis
2. Build the Docker image and test the script iteratively
3. Refine the script until it runs successfully in Docker
4. Output the final working bash script

**CRITICAL**: You MUST keep iterating until mcp__crsbench__check_test_sh returns success.
"""
        else:
            # Simple two-phase approach (analyze → generate)
            prompt = f"""Generate a test.sh script for the OSS-Fuzz benchmark `{benchmark_name}`.

# Context
- Benchmark directory: {benchmark_dir}
- **CRITICAL**: build.sh and test.sh are located at $SRC directory in the container
- **CRITICAL**: test.sh is executed from $SRC directory via `bash $SRC/test.sh`
- Project source code is mounted at $SRC/<project-name> in the container

# Test Analysis
```markdown
{test_analysis_md}
```

# Your Task
Use the appropriate skill to generate a working test.sh script based on the test analysis above.

Output ONLY the bash script content, starting with #!/bin/bash.
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
                "Use available skills to help with test.sh generation. "
                "\n\n"
                "**CRITICAL OUTPUT REQUIREMENT:**\n"
                "- Your final output MUST be an EXECUTABLE BASH SCRIPT\n"
                "- DO NOT output markdown, explanations, or analysis documents\n"
                "- The script must run successfully inside Docker container\n"
                "- DO NOT finish until the script executes successfully\n"
                "\n"
                "**CRITICAL EXECUTION ENVIRONMENT:**\n"
                "- **build.sh and test.sh are located at $SRC directory in the container**\n"
                "- **test.sh is executed from $SRC directory via `bash $SRC/test.sh`**\n"
                "- **Project source code is mounted at $SRC/<project-name>**\n"
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
                "You are a bash scripting expert. "
                "Use available skills to help with test.sh generation. "
                "Generate clean, working bash scripts following the patterns provided. "
                "You can use Read to examine files, Write to create files, Edit to modify files, "
                "WebSearch to research build system patterns, WebFetch for official docs, "
                "and TodoWrite to organize your work. "
                "\n\n"
                "**CRITICAL EXECUTION ENVIRONMENT:**\n"
                "- **build.sh and test.sh are located at $SRC directory in the container**\n"
                "- **test.sh is executed from $SRC directory via `bash $SRC/test.sh`**\n"
                "- **Project source code is mounted at $SRC/<project-name>**\n"
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
            "analysis_md_path": str,        # .agent/test_analysis.md
            "test_sh_gen_md_path": str,     # .agent/test_sh_gen.md (rationale)
            "agent_log_path": str,          # .agent/agent_log.txt
            "execution_log_path": str,      # .agent/test_sh_execution.log
            "test_sh_executed": bool,       # Whether test.sh ran successfully
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

    # Save analysis markdown to .agent directory
    agent_dir = os.path.join(benchmark_dir, ".agent")
    os.makedirs(agent_dir, exist_ok=True)

    analysis_md_path = os.path.join(agent_dir, "test_analysis.md")
    with open(analysis_md_path, "w") as f:
        f.write(test_analysis_md)

    if verbose:
        print(f"✅ Test analysis saved to {analysis_md_path}")

    # Step 2: Generate test.sh
    if verbose:
        mode_msg = "with Docker testing" if with_docker_testing else "two-phase"
        print(f"🔧 Generating test.sh script ({mode_msg})...")

    test_sh_content, agent_response_text, generation_log = generator.generate_test_sh_script_sync(
        test_analysis_md,
        benchmark_name,
        benchmark_dir,
        with_docker_testing,
        verbose
    )

    # Save agent response (rationale) to .agent/test_sh_gen.md
    test_sh_gen_md_path = os.path.join(agent_dir, "test_sh_gen.md")
    with open(test_sh_gen_md_path, "w") as f:
        f.write(f"""# test.sh Generation Response

Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Method: {"iterative Docker testing (MCP-enhanced)" if with_docker_testing else "two-phase analysis"}

## Agent Response

{agent_response_text}
""")

    if verbose:
        print(f"✅ Agent response saved to {test_sh_gen_md_path}")

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
        "analysis_md_path": analysis_md_path,
        "test_sh_gen_md_path": test_sh_gen_md_path,
        "agent_log_path": agent_log_path,
        "execution_log_path": execution_log_path,
        "test_sh_executed": execution_success,
        "message": f"Successfully generated test.sh for {benchmark_name}"
    }
