"""
vuln.yaml Generator for CRSBench benchmarks.

This module uses Claude Agent SDK to automatically generate vuln.yaml files
for CPVs (Challenge Project Vulnerabilities) that don't have them. It analyzes
crash logs, POV files, patches, and source code to generate accurate
vulnerability metadata.
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Dict, Any, List, Optional, Tuple
import yaml

from claude_agent_sdk import query, ClaudeAgentOptions


class VulnYamlGenerator:
    """
    Agent-based vuln.yaml generator using Claude Agent SDK.

    Uses LiteLLM proxy for Claude API access via LITELLM_BASE_URL and LITELLM_API_KEY.
    """

    def __init__(
        self,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929"
    ):
        """
        Initialize the vuln.yaml generator agent.

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
            phase_name: Name of the phase (e.g., "Vulnerability Analysis")

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

    async def analyze_vulnerability(
        self,
        project_dir: str,
        cpv_dir: str,
        cpv_id: str,
        harness_name: str,
        verbose: bool = False
    ) -> Tuple[str, str]:
        """
        Analyze vulnerability from crash logs, POV, and patches.

        Args:
            project_dir: Directory containing the project source code
            cpv_dir: Directory containing CPV files (logs/, blobs/, patches/)
            cpv_id: CPV identifier (e.g., "cpv_0")
            harness_name: Name of the harness
            verbose: Enable verbose logging

        Returns:
            Tuple of (analysis_markdown, agent_log)
        """
        prompt = f"""You are a security researcher analyzing a vulnerability.

# Task
Analyze the vulnerability for: **{cpv_id}** in harness **{harness_name}**

CPV directory: `{cpv_dir}`
Project source: `{project_dir}`

# OSS-Fuzz Context
Check the `.oss-fuzz/` directory in the project root for:
- **build.sh**: Shows how the project is built
- **Dockerfile**: Lists dependencies and build environment
- **.aixcc/meta.yaml**: Contains harness file information

# Analysis Strategy

## 1. Analyze Crash Log
**IMPORTANT**: The crash log is the primary source of vulnerability information.

**CRITICAL**:
- **ONLY** read crash log files from **THIS CPV's logs directory**: `{cpv_dir}/logs/`
- **DO NOT** search for or read crash logs from other CPVs or other directories
- **DO NOT** use Grep/Glob to search for crash logs outside `{cpv_dir}/logs/`
- Each CPV has its own crash logs - you must ONLY analyze THIS CPV's logs

Steps:
- Find and read crash log files in `{cpv_dir}/logs/` (and ONLY this directory)
  - Typical names: `pov_0.log`, `pov_1.log`, `crash.log`
  - Use: `ls {cpv_dir}/logs/` or `Glob` pattern: `{cpv_dir}/logs/*.log`
- Extract key information:
  - **Error type**: AddressSanitizer error (heap-buffer-overflow, use-after-free, etc.)
  - **Vulnerable function**: The function where the crash occurred (from stack trace)
  - **Vulnerable file**: The source file path (from stack trace)
  - **Line number**: Exact line where crash happened
  - **DEDUP_TOKEN**: Unique identifier for this crash
  - **Stack trace**: Full call stack for understanding control flow

Example crash log analysis:
```
ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 45 at 0x50400000033c
#0 __asan_memcpy /src/llvm-project/compiler-rt/lib/asan/...
#1 cr_buf_read /src/curl/lib/sendf.c:1298:5    <-- Vulnerable function
#2 Curl_creader_read /src/curl/lib/sendf.c:542:10
...
SUMMARY: AddressSanitizer: heap-buffer-overflow /src/curl/lib/sendf.c:1298:5 in cr_buf_read
```

From this, extract:
- Error type: heap-buffer-overflow
- File: lib/sendf.c (relative to project root)
- Function: cr_buf_read
- Line: 1298

## 2. Locate Vulnerable Code
- Use the file path from crash log to find the vulnerable source file in `{project_dir}`
- Read the vulnerable file around the crash line number
- Identify the exact vulnerability location
- Note the function name, line numbers, and code context

## 3. Analyze POV (Proof of Vulnerability)
- **ONLY** check `{cpv_dir}/blobs/` for POV files (THIS CPV's blobs directory only)
- These are test inputs that trigger the vulnerability
- Note if POV structure provides hints about vulnerability type

## 4. Analyze Patches (if available)
- **ONLY** check `{cpv_dir}/patches/` for patch files (THIS CPV's patches directory only)
- Analyze what was changed to fix the vulnerability
- This helps understand the root cause

## 5. Identify CWE (Common Weakness Enumeration)
Based on the vulnerability type, identify relevant CWEs:
- Heap buffer overflow → CWE-122, CWE-787
- Stack buffer overflow → CWE-121, CWE-787
- Use-after-free → CWE-416
- NULL pointer dereference → CWE-476
- Integer overflow → CWE-190
- Command injection → CWE-77
- Path traversal → CWE-22
- Format string → CWE-134

## 6. Generate Vulnerability Description
Write a clear description that includes:
- What type of vulnerability it is
- Where it occurs (file, function)
- What causes it (based on crash log and patches)
- Potential impact

# Output Format

Provide a markdown document with the following structure:

```markdown
# Vulnerability Analysis for {cpv_id}

## Summary
- **CPV ID**: {cpv_id}
- **Harness**: {harness_name}
- **Vulnerability Type**: <type from crash log>
- **Severity**: <High/Medium/Low based on type>

## Crash Log Analysis
- **Error Type**: <AddressSanitizer error type>
- **Crash Location**: <file>:<line>
- **Vulnerable Function**: <function name>
- **DEDUP Token**: <dedup token from crash log>

### Stack Trace
```
<relevant stack trace snippet>
```

## Vulnerable Code Location
- **File Path**: <path from project root>
- **Function**: <function name>
- **Line Range**: <start>-<end>
- **Column Range**: <start>-<end>

### Code Context
```<language>
<vulnerable code snippet>
```

## CWE Classification
- CWE-XXX: <description>
- CWE-YYY: <description>

## Vulnerability Description
<Clear description of the vulnerability, its cause, and impact>

## POV Analysis (if available)
<Analysis of proof-of-vulnerability test case>

## Patch Analysis (if available)
<Analysis of how the vulnerability was fixed>

## Recommendations
- Suggested CWEs: [CWE-XXX, CWE-YYY]
- Vulnerability name: <concise name>
```

# Important
- **ALWAYS read and analyze the crash log first** - it contains the most critical information
- **ONLY analyze crash logs from `{cpv_dir}/logs/`** - DO NOT search for crash logs in other CPVs
- Use Grep and Glob to find relevant **source code files** in `{project_dir}` (NOT other CPVs' crash logs)
- Read actual source code to verify vulnerability locations
- Cite specific file paths and line numbers
- Be precise about code locations (line and column numbers)

Now analyze the vulnerability and provide the markdown document.
"""

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            system_prompt=(
                "You are a thorough security researcher. "
                "Use Grep to search patterns in source code, Glob to find files, and Read to examine files. "
                "CRITICAL: ONLY analyze crash logs from the specified CPV's logs directory. "
                "DO NOT search for or read crash logs from other CPVs. "
                "ALWAYS start by analyzing crash logs from the current CPV only. "
                "Be precise and cite specific locations."
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
                print(f"❌ Vulnerability analyzer agent error: {e}")
                import traceback
                traceback.print_exc()
            result_text = f"# Error\n\nFailed to analyze vulnerability: {str(e)}"

        # Generate agent log
        agent_log = self._format_agent_log(messages, "Vulnerability Analysis")

        return result_text, agent_log

    async def generate_vuln_yaml(
        self,
        analysis_md: str,
        cpv_id: str,
        harness_name: str,
        verbose: bool = False
    ) -> Tuple[str, str]:
        """
        Generate vuln.yaml from vulnerability analysis.

        Args:
            analysis_md: Markdown document from analyze_vulnerability()
            cpv_id: CPV identifier
            harness_name: Name of the harness
            verbose: Enable verbose logging

        Returns:
            Tuple of (vuln_yaml_content, agent_log)
        """
        prompt = f"""You are generating a vuln.yaml file for a CRSBench vulnerability.

# Task
Generate vuln.yaml for: **{cpv_id}** in harness **{harness_name}**

Based on the following vulnerability analysis:

```markdown
{analysis_md}
```

# vuln.yaml Format

The output must be a valid YAML file with this structure:

```yaml
id: {cpv_id}

name: <Short, descriptive vulnerability name>

cwes:
- CWE-XXX
- CWE-YYY

description: |
  <Clear, multi-line description of the vulnerability.
  Include what causes it, where it occurs, and potential impact.>

locations:
- path_from_root: <relative/path/from/project/root.c>
  function_name: <vulnerable_function>
  startLine: <line_number>
  startColumn: <column_number>
  endLine: <line_number>
  endColumn: <column_number>
```

# Field Guidelines

## id
- Must be exactly: `{cpv_id}`

## name
- Short, descriptive name (max 80 chars)
- Examples:
  - "Heap buffer overflow in cr_buf_read"
  - "Use-after-free in HTTP chunk processing"
  - "NULL pointer dereference in JSON parser"
- DO NOT use generic names like "MOCK: cpv_0 vulnerability"

## cwes
- List of relevant CWE numbers
- Include 1-3 most relevant CWEs
- Common CWEs:
  - CWE-787: Out-of-bounds Write
  - CWE-125: Out-of-bounds Read
  - CWE-416: Use After Free
  - CWE-476: NULL Pointer Dereference
  - CWE-190: Integer Overflow
  - CWE-122: Heap-based Buffer Overflow
  - CWE-121: Stack-based Buffer Overflow

## description
- Clear explanation of the vulnerability
- Include:
  - What type of vulnerability it is
  - Where it occurs (file, function)
  - What causes it
  - Potential security impact
- Use "|" for multi-line strings
- Be specific and technical

## locations
- At least one location (the primary vulnerable code location)
- **path_from_root**: Relative path from project root (NOT absolute path)
  - Remove `/src/curl/` prefix if present → use `lib/sendf.c`
  - Remove `/src/pdfbox/` prefix if present → use `pdfbox/src/main/java/...`
- **function_name**: Exact function name from crash log
- **startLine/endLine**: Line range of vulnerable code
  - If exact range unknown, use same line for both
- **startColumn/endColumn**: Column range
  - If unknown, use 0 for both

# Instructions
1. Extract CWE numbers from the analysis
2. Create a concise, descriptive name
3. Write a clear description based on the analysis
4. Extract code location information
5. Format as valid YAML

# Output
Provide ONLY the vuln.yaml content. No explanation, no markdown fences, just the raw YAML.

Generate the vuln.yaml file now:
"""

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=["Read"],  # Only reading for context
            system_prompt=(
                "You are a YAML generation expert. "
                "Generate clean, valid YAML following the exact format specified. "
                "Output only the YAML content, no extra text."
            ),
            env={
                "ANTHROPIC_BASE_URL": self.litellm_base_url,
                "ANTHROPIC_AUTH_TOKEN": self.litellm_api_key,
            }
        )

        yaml_content = ""
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
                                yaml_content += block.text
                    elif isinstance(message.content, str):
                        yaml_content += message.content

        except Exception as e:
            if verbose:
                print(f"❌ YAML generator agent error: {e}")
                import traceback
                traceback.print_exc()
            # Fallback YAML
            yaml_content = f"""id: {cpv_id}

name: 'Error: Failed to generate vuln.yaml'

cwes: []

description: |
  Error: Vulnerability analysis failed.

locations:
- path_from_root: 'unknown'
  function_name: unknown
  startLine: 0
  startColumn: 0
  endLine: 0
  endColumn: 0
"""

        # Clean up the YAML content
        yaml_content = yaml_content.strip()

        # Remove markdown code fences if present
        if yaml_content.startswith("```yaml"):
            yaml_content = yaml_content[7:]
        if yaml_content.startswith("```"):
            yaml_content = yaml_content[3:]
        if yaml_content.endswith("```"):
            yaml_content = yaml_content[:-3]

        yaml_content = yaml_content.strip()

        # Generate agent log
        agent_log = self._format_agent_log(messages, "vuln.yaml Generation")

        return yaml_content, agent_log

    def analyze_vulnerability_sync(
        self,
        project_dir: str,
        cpv_dir: str,
        cpv_id: str,
        harness_name: str,
        verbose: bool = False
    ) -> Tuple[str, str]:
        """Synchronous wrapper for analyze_vulnerability.

        Returns:
            Tuple of (analysis_markdown, agent_log)
        """
        return asyncio.run(
            self.analyze_vulnerability(project_dir, cpv_dir, cpv_id, harness_name, verbose)
        )

    def generate_vuln_yaml_sync(
        self,
        analysis_md: str,
        cpv_id: str,
        harness_name: str,
        verbose: bool = False
    ) -> Tuple[str, str]:
        """Synchronous wrapper for generate_vuln_yaml.

        Returns:
            Tuple of (vuln_yaml_content, agent_log)
        """
        return asyncio.run(
            self.generate_vuln_yaml(analysis_md, cpv_id, harness_name, verbose)
        )


def generate_vuln_yaml_for_cpv(
    benchmark_name: str,
    benchmark_dir: str,
    harness_name: str,
    cpv_id: str,
    project_dir: str,
    litellm_base_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
    force: bool = False,
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Generate vuln.yaml for a specific CPV.

    Args:
        benchmark_name: Name of the benchmark
        benchmark_dir: Path to benchmark directory (contains .aixcc/)
        harness_name: Name of the harness
        cpv_id: CPV identifier (e.g., "cpv_0")
        project_dir: Path to project source repository
        litellm_base_url: Optional LiteLLM proxy URL
        litellm_api_key: Optional LiteLLM API key
        force: Overwrite existing vuln.yaml
        verbose: Enable verbose logging

    Returns:
        Dictionary with generation results:
        {
            "success": bool,
            "vuln_yaml_path": str,
            "analysis_md_path": str,
            "agent_log_path": str,
            "message": str
        }
    """
    # Validate directories
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

    # Locate CPV directory
    cpv_dir = os.path.join(benchmark_dir, ".aixcc", harness_name, cpv_id)
    if not os.path.isdir(cpv_dir):
        return {
            "success": False,
            "message": f"CPV directory not found: {cpv_dir}"
        }

    # Check if vuln.yaml already exists
    vuln_yaml_path = os.path.join(cpv_dir, "vuln.yaml")
    is_temporary = False

    if os.path.exists(vuln_yaml_path):
        # Check if it's a temporary/MOCK file
        try:
            with open(vuln_yaml_path, 'r') as f:
                content = f.read()
                # Check for MOCK or TBD markers indicating temporary content
                if 'MOCK:' in content or '(TBD)' in content:
                    is_temporary = True
                    if verbose:
                        print(f"   Found temporary vuln.yaml (contains MOCK/TBD), will regenerate...")
        except Exception as e:
            if verbose:
                print(f"   Warning: Could not read existing vuln.yaml: {e}")

        # If not temporary and not force, don't overwrite
        if not is_temporary and not force:
            return {
                "success": False,
                "message": f"vuln.yaml already exists at {vuln_yaml_path}. Use --force to overwrite."
            }

    # Create generator
    generator = VulnYamlGenerator(
        litellm_base_url=litellm_base_url,
        litellm_api_key=litellm_api_key
    )

    # Copy OSS-Fuzz files to project directory
    if verbose:
        print(f"📋 Copying OSS-Fuzz files to {project_dir}/.oss-fuzz/...")

    from crsbench.migration.test_sh_generator import _copy_benchmark_files_to_project
    _copy_benchmark_files_to_project(benchmark_dir, project_dir, verbose)

    # Step 1: Analyze vulnerability
    if verbose:
        print(f"🔍 Analyzing vulnerability {cpv_id} in {harness_name}...")

    analysis_md, analysis_log = generator.analyze_vulnerability_sync(
        project_dir, cpv_dir, cpv_id, harness_name, verbose
    )

    # Save analysis markdown
    analysis_md_path = os.path.join(cpv_dir, "vuln_analysis.md")
    with open(analysis_md_path, "w") as f:
        f.write(analysis_md)

    if verbose:
        print(f"✅ Vulnerability analysis saved to {analysis_md_path}")

    # Step 2: Generate vuln.yaml
    if verbose:
        print(f"🔧 Generating vuln.yaml...")

    vuln_yaml_content, generation_log = generator.generate_vuln_yaml_sync(
        analysis_md, cpv_id, harness_name, verbose
    )

    # Save vuln.yaml
    with open(vuln_yaml_path, "w") as f:
        f.write(vuln_yaml_content)

    if verbose:
        print(f"✅ vuln.yaml generated at {vuln_yaml_path}")

    # Step 3: Save combined agent log
    agent_log_path = os.path.join(cpv_dir, "vuln_agent_log.txt")
    combined_log = f"""# vuln.yaml Generation Agent Log
Generated: {datetime.now().isoformat()}
Benchmark: {benchmark_name}
Harness: {harness_name}
CPV: {cpv_id}
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
        "vuln_yaml_path": vuln_yaml_path,
        "analysis_md_path": analysis_md_path,
        "agent_log_path": agent_log_path,
        "message": f"Successfully generated vuln.yaml for {cpv_id}"
    }
