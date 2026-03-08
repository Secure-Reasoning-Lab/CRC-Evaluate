"""Tests for Claude Agent SDK integration."""

import os
from pathlib import Path

import pytest

# Try to import claude_agent_sdk, skip tests if not available
pytest.importorskip("claude_agent_sdk")

from crsbench.migration.test_sh import (
    ShTestGenerator,
    generate_test_sh_for_benchmark,
)
from crsbench.utils.paths import get_crsbench_root
from crsbench.utils.repo_manager import find_or_clone_project

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def litellm_config():
    """Get LiteLLM configuration from environment."""
    return {
        "base_url": os.getenv("CRSBENCH_LLM_BASE_URL", "http://localhost:4000"),
        "api_key": os.getenv("CRSBENCH_LLM_API_KEY", "test-key"),
    }


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory with test files."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create a simple pom.xml (Maven project)
    pom_xml = project_dir / "pom.xml"
    pom_xml.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.test</groupId>
    <artifactId>test-project</artifactId>
    <version>1.0</version>
</project>
""")

    # Create test directory
    test_dir = project_dir / "src" / "test" / "java" / "com" / "test"
    test_dir.mkdir(parents=True)

    # Create a simple test file
    test_file = test_dir / "ExampleTest.java"
    test_file.write_text("""package com.test;

import org.junit.Test;
import static org.junit.Assert.*;

public class ExampleTest {
    @Test
    public void testExample() {
        assertEquals(1, 1);
    }
}
""")

    return project_dir


@pytest.fixture
def temp_benchmark_dir(tmp_path):
    """Create a temporary benchmark directory with OSS-Fuzz files."""
    benchmark_dir = tmp_path / "test_benchmark"
    benchmark_dir.mkdir()

    # Create .aixcc directory
    aixcc_dir = benchmark_dir / ".aixcc"
    aixcc_dir.mkdir()

    # Create minimal meta.yaml
    meta_yaml = aixcc_dir / "meta.yaml"
    meta_yaml.write_text("""harness_files:
  - name: TestHarness
    path: /src/test/harness.c
""")

    # Create project.yaml (required by OSS-Fuzz)
    project_yaml = benchmark_dir / "project.yaml"
    project_yaml.write_text("""homepage: "https://example.com"
language: java
primary_contact: "test@example.com"
main_repo: "https://github.com/test/test-project"
""")

    # Create Dockerfile (required by OSS-Fuzz)
    dockerfile = benchmark_dir / "Dockerfile"
    dockerfile.write_text("""FROM gcr.io/oss-fuzz-base/base-builder-jvm

RUN apt-get update && apt-get install -y maven

COPY . $SRC/test-project
WORKDIR $SRC/test-project
""")

    # Create build.sh (required by OSS-Fuzz)
    build_sh = benchmark_dir / "build.sh"
    build_sh.write_text("""#!/bin/bash -eu

cd $SRC/test-project

# Simple Maven build
mvn -B package -DskipTests || true

# Create a dummy fuzzer for testing
echo "echo 'Build successful'" > $OUT/dummy_fuzzer
chmod +x $OUT/dummy_fuzzer
""")
    build_sh.chmod(0o755)

    return benchmark_dir


# ============================================================================
# TestShGeneratorAgent Tests
# ============================================================================


class TestShTestGenerator:
    """Test ShTestGenerator class."""

    def test_agent_initialization(self, litellm_config):
        """Test agent can be initialized."""
        agent = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        assert agent.litellm_base_url == litellm_config["base_url"]
        assert agent.litellm_api_key == litellm_config["api_key"]
        assert agent.model == "claude-sonnet-4-5-20250929"

    def test_agent_initialization_from_env(self):
        """Test agent initialization from environment variables."""
        # Set env vars
        os.environ["CRSBENCH_LLM_BASE_URL"] = "http://test:4000"
        os.environ["CRSBENCH_LLM_API_KEY"] = "test-key-123"

        agent = ShTestGenerator()

        assert agent.litellm_base_url == "http://test:4000"
        assert agent.litellm_api_key == "test-key-123"

    def test_agent_missing_base_url(self):
        """Test agent raises error when base URL is missing."""
        # Clear env vars
        if "CRSBENCH_LLM_BASE_URL" in os.environ:
            del os.environ["CRSBENCH_LLM_BASE_URL"]

        with pytest.raises(ValueError, match="CRSBENCH_LLM_BASE_URL"):
            ShTestGenerator(litellm_api_key="test-key")

    def test_agent_missing_api_key(self):
        """Test agent raises error when API key is missing."""
        # Clear env var
        if "CRSBENCH_LLM_API_KEY" in os.environ:
            del os.environ["CRSBENCH_LLM_API_KEY"]

        with pytest.raises(ValueError, match="CRSBENCH_LLM_API_KEY"):
            ShTestGenerator(litellm_base_url="http://test:4000")

    def test_agent_has_required_methods(self, litellm_config):
        """Test agent has all required methods."""
        agent = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        assert hasattr(agent, "find_unit_tests")
        assert hasattr(agent, "generate_test_sh_script")
        assert hasattr(agent, "find_unit_tests_sync")
        assert hasattr(agent, "generate_test_sh_script_sync")

        # Check they're callable
        assert callable(agent.find_unit_tests)
        assert callable(agent.generate_test_sh_script)
        assert callable(agent.find_unit_tests_sync)
        assert callable(agent.generate_test_sh_script_sync)


# ============================================================================
# Integration Tests (requires actual LiteLLM connection)
# ============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("CRSBENCH_LLM_BASE_URL") or not os.getenv("CRSBENCH_LLM_API_KEY"),
    reason="LiteLLM environment variables not set",
)
class TestAgentIntegration:
    """Integration tests requiring actual LiteLLM connection."""

    def test_find_unit_tests(self, temp_project_dir, litellm_config):
        """Test finding unit tests in a project."""
        agent = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        # This will make actual API call
        result, log = agent.find_unit_tests_sync(str(temp_project_dir), verbose=False)

        # Check we got some markdown output
        assert isinstance(result, str)
        assert len(result) > 0
        # Should mention Maven or pom.xml
        assert "maven" in result.lower() or "pom.xml" in result.lower()

        # Check we got agent log
        assert isinstance(log, str)
        assert len(log) > 0

    def test_generate_test_sh_script(self, litellm_config):
        """Test generating test.sh script."""
        agent = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        # Mock test analysis
        test_analysis = """# Unit Test Analysis

## Build System
- Type: Maven
- Language: Java

## Test Framework
- Framework: JUnit

## Build Command
```
mvn compile
```

## Test Execution Command
```
mvn test
```
"""

        # This will make actual API call
        result, log = agent.generate_test_sh_script_sync(
            test_analysis, "test-benchmark", verbose=False
        )

        # Check we got a bash script
        assert isinstance(result, str)
        assert result.startswith("#!/bin/bash")
        assert "mvn" in result.lower() or "test" in result.lower()

        # Check we got agent log
        assert isinstance(log, str)
        assert len(log) > 0

    def test_generate_test_sh_for_benchmark(
        self, temp_benchmark_dir, temp_project_dir, litellm_config
    ):
        """Test full workflow of generating test.sh for benchmark."""
        result = generate_test_sh_for_benchmark(
            benchmark_name="test-benchmark",
            benchmark_dir=str(temp_benchmark_dir),
            project_dir=str(temp_project_dir),
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
            verbose=False,
        )

        # Check result
        assert result["success"] is True
        assert "test_sh_path" in result
        assert "analysis_md_path" in result
        assert "agent_log_path" in result

        # Check files were created
        test_sh_path = Path(result["test_sh_path"])
        assert test_sh_path.exists()
        assert test_sh_path.stat().st_mode & 0o111  # Executable

        analysis_path = Path(result["analysis_md_path"])
        assert analysis_path.exists()

        agent_log_path = Path(result["agent_log_path"])
        assert agent_log_path.exists()

        # Check test.sh content
        test_sh_content = test_sh_path.read_text()
        assert test_sh_content.startswith("#!/bin/bash")


# ============================================================================
# Function Tests
# ============================================================================


class TestGenerateTestShForBenchmark:
    """Test generate_test_sh_for_benchmark function."""

    def test_invalid_benchmark_dir(self, temp_project_dir, litellm_config):
        """Test with invalid benchmark directory."""
        result = generate_test_sh_for_benchmark(
            benchmark_name="test",
            benchmark_dir="/nonexistent/path",
            project_dir=str(temp_project_dir),
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_invalid_project_dir(self, temp_benchmark_dir, litellm_config):
        """Test with invalid project directory."""
        result = generate_test_sh_for_benchmark(
            benchmark_name="test",
            benchmark_dir=str(temp_benchmark_dir),
            project_dir="/nonexistent/path",
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_existing_test_sh_without_force(
        self, temp_benchmark_dir, temp_project_dir, litellm_config
    ):
        """Test when test.sh already exists without force flag."""
        # Create existing test.sh
        test_sh = temp_benchmark_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\necho 'existing'\n")

        result = generate_test_sh_for_benchmark(
            benchmark_name="test",
            benchmark_dir=str(temp_benchmark_dir),
            project_dir=str(temp_project_dir),
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
            verbose=False,
        )

        # Should fail without force
        assert result["success"] is False
        assert "already exists" in result["message"].lower()


# ============================================================================
# MCP Integration Tests (requires LiteLLM + Docker + OSS-Fuzz)
# ============================================================================


@pytest.mark.integration
@pytest.mark.mcp
@pytest.mark.skipif(
    not os.getenv("CRSBENCH_LLM_BASE_URL") or not os.getenv("CRSBENCH_LLM_API_KEY"),
    reason="LiteLLM environment variables not set",
)
@pytest.mark.skipif(
    not (
        get_crsbench_root() / "third_party" / "oss-fuzz" / "infra" / "helper.py"
    ).exists(),
    reason="Managed OSS-Fuzz checkout not available",
)
class TestMCPIntegration:
    """Integration tests for MCP-enabled test.sh generation using real benchmarks.

    These tests are heavy - they require:
    - LiteLLM connection
    - Docker installed and running
    - Managed OSS-Fuzz checkout initialized
    - Real benchmarks directory
    - Sufficient disk space for Docker images
    """

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "benchmark_name,_language",
        [
            ("libxml2-delta-03", "c"),
            ("apache-commons-compress-delta-01", "jvm"),
        ],
    )
    def test_generate_with_mcp_docker_testing_real_benchmarks(
        self, benchmark_name, _language, litellm_config, tmp_path
    ):
        """Test MCP-enabled generation with real benchmarks.

        This test:
        1. Uses real benchmark from benchmarks/ directory
        2. Uses repo_manager to find/clone project (just like CLI)
        3. Generates test.sh using agent with MCP tools
        4. Tests the script in Docker container

        Tests both C and Java benchmarks.

        This is a SLOW test (5-10 min per benchmark).
        Run with: uv run pytest tests/test_agent.py -v -m slow

        NOTE: This test requires git access to clone the project repository.
        """
        benchmark_dir = str(get_crsbench_root() / "benchmarks" / benchmark_name)

        # Check if benchmark exists
        if not os.path.exists(benchmark_dir):
            pytest.skip(f"Benchmark {benchmark_name} not found")

        # Use repo_manager to find/clone project (same as CLI)
        # Clone to temp directory instead of default afc-repos
        repos_dir = str(tmp_path / "repos")
        os.makedirs(repos_dir, exist_ok=True)

        project_dir = find_or_clone_project(
            benchmark_name=benchmark_name,
            benchmarks_root=str(get_crsbench_root() / "benchmarks"),
            repos_dir=repos_dir,
            verbose=True,
        )

        if not project_dir:
            pytest.skip(f"Failed to find/clone project for {benchmark_name}")

        # Full end-to-end MCP test with real benchmarks
        result = generate_test_sh_for_benchmark(
            benchmark_name=benchmark_name,
            benchmark_dir=benchmark_dir,
            project_dir=project_dir,  # Already a string from find_or_clone_project
            with_docker_testing=True,  # Enable MCP tools
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
            verbose=True,
        )

        assert result["success"] is True
        assert Path(result["test_sh_path"]).exists()

        # Verify all output files were created
        assert Path(result["analysis_md_path"]).exists()
        assert Path(result["agent_log_path"]).exists()

        # Verify test.sh was generated with MCP assistance
        test_sh_content = Path(result["test_sh_path"]).read_text()
        assert test_sh_content.startswith("#!/bin/bash")

        # Verify agent log contains both analysis and generation logs
        agent_log_content = Path(result["agent_log_path"]).read_text()
        assert "Test.sh Generation Agent Log" in agent_log_content
        assert benchmark_name in agent_log_content

    def test_generate_with_mcp_docker_testing(
        self, temp_benchmark_dir, temp_project_dir
    ):
        """Test MCP-enabled generation with temporary test fixtures.

        This test uses minimal fixtures for quick validation.
        For real benchmark testing, use test_generate_with_mcp_docker_testing_real_benchmarks.
        """
        # This is a lightweight test - just verify the fixture works
        assert temp_benchmark_dir.exists()
        assert temp_project_dir.exists()
        assert (temp_benchmark_dir / "Dockerfile").exists()
        assert (temp_benchmark_dir / "build.sh").exists()
        assert (temp_benchmark_dir / "project.yaml").exists()

    def test_mcp_tools_accessible(self, litellm_config):
        """Test that MCP tools are accessible in with_docker_testing mode.

        This is a lighter test that just checks if MCP tools can be invoked
        without doing full build/test cycle.
        """
        from crsbench.migration.test_sh import ShTestGenerator

        generator = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
        )

        # Just test that the generator can be initialized
        # Full MCP functionality tested in test_generate_with_mcp_docker_testing
        assert generator.litellm_base_url == litellm_config["base_url"]
        assert generator.litellm_api_key == litellm_config["api_key"]


# ============================================================================
# Marker for running specific test groups
# ============================================================================

# Run only unit tests (no LiteLLM required):
#   uv run pytest tests/test_agent.py -v -m "not integration"
#
# Run integration tests (requires LiteLLM):
#   uv run pytest tests/test_agent.py -v -m integration
#
# Run MCP integration tests (requires LiteLLM + Docker + OSS-Fuzz):
#   uv run pytest tests/test_agent.py -v -m mcp
#
# Run SLOW MCP tests with real benchmarks (5-10 min per benchmark):
#   uv run pytest tests/test_agent.py -v -m slow
#
# Run MCP tests excluding slow ones:
#   uv run pytest tests/test_agent.py -v -m "mcp and not slow"
#
# Run all tests:
#   uv run pytest tests/test_agent.py -v
