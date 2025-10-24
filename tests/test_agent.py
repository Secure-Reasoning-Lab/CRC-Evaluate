"""Tests for Claude Agent SDK integration."""

import os
import pytest
import tempfile
from pathlib import Path

# Try to import claude_agent_sdk, skip tests if not available
pytest.importorskip("claude_agent_sdk")

from crsbench.migration.test_sh_generator import (
    ShTestGenerator,
    generate_test_sh_for_benchmark
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def litellm_config():
    """Get LiteLLM configuration from environment."""
    return {
        "base_url": os.getenv("LITELLM_BASE_URL", "http://localhost:4000"),
        "api_key": os.getenv("LITELLM_API_KEY", "test-key")
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
    """Create a temporary benchmark directory."""
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
            litellm_api_key=litellm_config["api_key"]
        )

        assert agent.litellm_base_url == litellm_config["base_url"]
        assert agent.litellm_api_key == litellm_config["api_key"]
        assert agent.model == "claude-sonnet-4-5-20250929"

    def test_agent_initialization_from_env(self):
        """Test agent initialization from environment variables."""
        # Set env vars
        os.environ["LITELLM_BASE_URL"] = "http://test:4000"
        os.environ["LITELLM_API_KEY"] = "test-key-123"

        agent = ShTestGenerator()

        assert agent.litellm_base_url == "http://test:4000"
        assert agent.litellm_api_key == "test-key-123"

    def test_agent_missing_base_url(self):
        """Test agent raises error when base URL is missing."""
        # Clear env var
        if "LITELLM_BASE_URL" in os.environ:
            del os.environ["LITELLM_BASE_URL"]

        with pytest.raises(ValueError, match="LITELLM_BASE_URL"):
            ShTestGenerator(litellm_api_key="test-key")

    def test_agent_missing_api_key(self):
        """Test agent raises error when API key is missing."""
        # Clear env var
        if "LITELLM_API_KEY" in os.environ:
            del os.environ["LITELLM_API_KEY"]

        with pytest.raises(ValueError, match="LITELLM_API_KEY"):
            ShTestGenerator(litellm_base_url="http://test:4000")

    def test_agent_has_required_methods(self, litellm_config):
        """Test agent has all required methods."""
        agent = ShTestGenerator(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"]
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
    not os.getenv("LITELLM_BASE_URL") or not os.getenv("LITELLM_API_KEY"),
    reason="LiteLLM environment variables not set"
)
class TestAgentIntegration:
    """Integration tests requiring actual LiteLLM connection."""

    def test_find_unit_tests(self, temp_project_dir, litellm_config):
        """Test finding unit tests in a project."""
        agent = TestShGeneratorAgent(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"]
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
        agent = TestShGeneratorAgent(
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"]
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
            test_analysis,
            "test-benchmark",
            verbose=False
        )

        # Check we got a bash script
        assert isinstance(result, str)
        assert result.startswith("#!/bin/bash")
        assert "mvn" in result.lower() or "test" in result.lower()

        # Check we got agent log
        assert isinstance(log, str)
        assert len(log) > 0

    def test_generate_test_sh_for_benchmark(
        self,
        temp_benchmark_dir,
        temp_project_dir,
        litellm_config
    ):
        """Test full workflow of generating test.sh for benchmark."""
        result = generate_test_sh_for_benchmark(
            benchmark_name="test-benchmark",
            benchmark_dir=str(temp_benchmark_dir),
            project_dir=str(temp_project_dir),
            litellm_base_url=litellm_config["base_url"],
            litellm_api_key=litellm_config["api_key"],
            verbose=False
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
            litellm_api_key=litellm_config["api_key"]
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
            litellm_api_key=litellm_config["api_key"]
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_existing_test_sh_without_force(
        self,
        temp_benchmark_dir,
        temp_project_dir,
        litellm_config
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
            verbose=False
        )

        # Should fail without force
        assert result["success"] is False
        assert "already exists" in result["message"].lower()


# ============================================================================
# Marker for running specific test groups
# ============================================================================

# Run only unit tests (no LiteLLM required):
#   uv run pytest tests/test_agent.py -v -m "not integration"
#
# Run integration tests (requires LiteLLM):
#   uv run pytest tests/test_agent.py -v -m integration
#
# Run all tests:
#   uv run pytest tests/test_agent.py -v
