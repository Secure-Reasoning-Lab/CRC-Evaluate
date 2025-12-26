"""Tests for hint generation module.

This test suite verifies the SARIF hint generation functionality
for different hint levels.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from crsbench.hint_generation.cwe_mapping import get_cwe_name, get_general_class
from crsbench.hint_generation.sarif_generator_simple import (
    HintLevel,
    SarifHintGenerator,
    VulnInfo,
    generate_hints_for_benchmark,
)


# Test fixtures
@pytest.fixture
def sample_vuln_data() -> Dict[str, Any]:
    """Sample vulnerability data for testing."""
    return {
        "id": "cpv_test",
        "name": "Test Buffer Overflow",
        "cwes": ["CWE-122", "CWE-787"],
        "description": "A test heap buffer overflow vulnerability",
        "locations": [
            {
                "path_from_root": "test.c",
                "function_name": "vulnerable_function",
                "startLine": 100,
                "startColumn": 5,
                "endLine": 105,
                "endColumn": 10,
            },
            {
                "path_from_root": "helper.c",
                "function_name": "helper_function",
                "startLine": 50,
                "startColumn": 1,
                "endLine": 55,
                "endColumn": 1,
            },
        ],
    }


@pytest.fixture
def temp_vuln_yaml(sample_vuln_data) -> Path:
    """Create a temporary vuln.yaml file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(sample_vuln_data, f)
        temp_path = Path(f.name)

    yield temp_path

    # Cleanup
    temp_path.unlink()


@pytest.fixture
def temp_output_dir() -> Path:
    """Create a temporary output directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestCWEMapping:
    """Tests for CWE mapping functionality."""

    def test_known_cwe_mapping(self):
        """Test mapping of known CWE IDs."""
        general_class, description = get_general_class("CWE-122")
        assert general_class == "Memory Safety Issue"
        assert description == "Heap-based buffer overflow"

    def test_cwe_without_prefix(self):
        """Test CWE mapping without 'CWE-' prefix."""
        general_class, description = get_general_class("122")
        assert general_class == "Memory Safety Issue"
        assert description == "Heap-based buffer overflow"

    def test_unknown_cwe(self):
        """Test mapping of unknown CWE ID."""
        general_class, description = get_general_class("CWE-99999")
        assert general_class == "Unknown Vulnerability"
        assert description == "Unknown vulnerability type"

    def test_multiple_cwe_categories(self):
        """Test CWE mappings across different categories."""
        test_cases = [
            ("CWE-122", "Memory Safety Issue"),
            ("CWE-416", "Memory Management Issue"),
            ("CWE-89", "Input Validation Issue"),
            ("CWE-362", "Concurrency Issue"),
        ]

        for cwe_id, expected_class in test_cases:
            general_class, _ = get_general_class(cwe_id)
            assert general_class == expected_class

    def test_get_cwe_name(self):
        """Test getting CWE descriptive name."""
        name = get_cwe_name("CWE-787")
        assert name == "Out-of-bounds write"


class TestVulnInfo:
    """Tests for VulnInfo class."""

    def test_vuln_info_initialization(self, sample_vuln_data):
        """Test VulnInfo initialization from dictionary."""
        vuln_info = VulnInfo(sample_vuln_data)

        assert vuln_info.id == "cpv_test"
        assert vuln_info.name == "Test Buffer Overflow"
        assert vuln_info.cwes == ["CWE-122", "CWE-787"]
        assert len(vuln_info.locations) == 2
        assert vuln_info.locations[0]["function_name"] == "vulnerable_function"

    def test_vuln_info_from_yaml(self, temp_vuln_yaml):
        """Test loading VulnInfo from YAML file."""
        vuln_info = VulnInfo.from_yaml(temp_vuln_yaml)

        assert vuln_info.id == "cpv_test"
        assert vuln_info.name == "Test Buffer Overflow"
        assert "CWE-122" in vuln_info.cwes

    def test_vuln_info_with_missing_fields(self):
        """Test VulnInfo with missing optional fields."""
        minimal_data = {
            "id": "minimal_test",
        }
        vuln_info = VulnInfo(minimal_data)

        assert vuln_info.id == "minimal_test"
        assert vuln_info.name == "Unknown vulnerability"
        assert vuln_info.cwes == []
        assert vuln_info.locations == []


class TestSarifHintGenerator:
    """Tests for SARIF hint generator."""

    def test_generator_initialization(self, sample_vuln_data):
        """Test SarifHintGenerator initialization."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        assert generator.vuln_info == vuln_info
        assert generator.tool_name == "CRSBench-HintGenerator"
        assert generator.tool_version == "1.0.0"

    def test_level_1_generation(self, sample_vuln_data):
        """Test SARIF generation at Level 1 (general class only)."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.GENERAL_CLASS)
        sarif = json.loads(sarif_json)

        # Verify basic SARIF structure
        assert sarif["version"] == "2.1.0"
        assert "$schema" in sarif
        assert "runs" in sarif
        assert len(sarif["runs"]) == 1

        run = sarif["runs"][0]
        assert "tool" in run
        assert "results" in run

        # Verify tool info
        tool = run["tool"]["driver"]
        assert tool["name"] == "CRSBench-HintGenerator"

        # Verify rules (Level 1 should have unique general classes)
        # CWE-122 and CWE-787 both map to "Memory Safety Issue", so only 1 rule
        rules = tool["rules"]
        assert len(rules) == 1  # One unique general class in sample data
        assert rules[0]["name"] == "Memory Safety Issue"

        # Verify results (Level 1 deduplicates to unique general classes)
        results = run["results"]
        assert len(results) == 1  # One unique general class
        assert "Memory Safety Issue" in results[0]["message"]["text"]

        # Level 1 should NOT have locations
        assert "locations" not in results[0]

    def test_level_2_generation(self, sample_vuln_data):
        """Test SARIF generation at Level 2 (specific type)."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.SPECIFIC_TYPE)
        sarif = json.loads(sarif_json)

        run = sarif["runs"][0]
        rules = run["tool"]["driver"]["rules"]
        results = run["results"]

        # Level 2 should have specific CWE info
        assert "CWE-122" in rules[0]["name"]
        assert "CWE-122" in results[0]["message"]["text"]

        # Level 2 should NOT have locations
        assert "locations" not in results[0]

    def test_level_3_generation(self, sample_vuln_data):
        """Test SARIF generation at Level 3 (with function names)."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_FUNCTION)
        sarif = json.loads(sarif_json)

        run = sarif["runs"][0]
        results = run["results"]

        # Level 3 should include function names in message
        assert "vulnerable_function" in results[0]["message"]["text"]
        assert "helper_function" in results[0]["message"]["text"]

        # Level 3 should have locations but WITHOUT line numbers
        assert "locations" in results[0]
        locations = results[0]["locations"]
        assert len(locations) == 2
        assert locations[0]["physicalLocation"]["artifactLocation"]["uri"] == "test.c"
        # Should NOT have region info at level 3
        assert "region" not in locations[0]["physicalLocation"]

        # Level 3 should have logicalLocations with function name
        assert "logicalLocations" in locations[0]
        logical_locs = locations[0]["logicalLocations"]
        assert len(logical_locs) == 1
        assert logical_locs[0]["name"] == "vulnerable_function"
        assert logical_locs[0]["kind"] == "function"

    def test_level_4_generation(self, sample_vuln_data):
        """Test SARIF generation at Level 4 (with line numbers)."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        run = sarif["runs"][0]
        results = run["results"]

        # Level 4 should include location info in message
        assert "test.c:100-105" in results[0]["message"]["text"]

        # Level 4 should have locations WITH line numbers
        assert "locations" in results[0]
        locations = results[0]["locations"]
        assert len(locations) == 2

        # Verify region info
        region = locations[0]["physicalLocation"]["region"]
        assert region["startLine"] == 100
        assert region["endLine"] == 105
        assert region["startColumn"] == 5
        assert region["endColumn"] == 10

        # Level 4 should also have logicalLocations with function name
        assert "logicalLocations" in locations[0]
        logical_locs = locations[0]["logicalLocations"]
        assert len(logical_locs) == 1
        assert logical_locs[0]["name"] == "vulnerable_function"
        assert logical_locs[0]["kind"] == "function"

    def test_multiple_cwes(self, sample_vuln_data):
        """Test handling multiple CWEs in single vulnerability."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.SPECIFIC_TYPE)
        sarif = json.loads(sarif_json)

        run = sarif["runs"][0]
        rules = run["tool"]["driver"]["rules"]
        results = run["results"]

        # Should have rules and results for both CWEs (CWE-122 and CWE-787)
        assert len(rules) == 2
        assert len(results) == 2
        assert rules[0]["id"] == "CWE-122"
        assert rules[1]["id"] == "CWE-787"

        # Each result should have deduplicated locations
        # Both results reference same locations but no duplication within each result
        assert "CWE-122" in results[0]["message"]["text"]
        assert "CWE-787" in results[1]["message"]["text"]


class TestGenerateHintsForBenchmark:
    """Tests for benchmark hint generation function."""

    def test_generate_all_levels(self, temp_vuln_yaml, temp_output_dir):
        """Test generating hints for all levels."""
        output_files = generate_hints_for_benchmark(
            vuln_yaml_path=temp_vuln_yaml,
            output_dir=temp_output_dir,
        )

        # Should generate files for all 5 levels
        assert len(output_files) == 5

        for level in HintLevel:
            assert level in output_files
            file_path = output_files[level]
            assert file_path.exists()
            assert file_path.name == f"level_{level}.sarif"

            # Verify file is valid JSON
            with open(file_path, "r") as f:
                sarif = json.load(f)
                assert sarif["version"] == "2.1.0"

    def test_generate_specific_levels(self, temp_vuln_yaml, temp_output_dir):
        """Test generating hints for specific levels only."""
        levels = [HintLevel.GENERAL_CLASS, HintLevel.WITH_LINES]
        output_files = generate_hints_for_benchmark(
            vuln_yaml_path=temp_vuln_yaml,
            output_dir=temp_output_dir,
            levels=levels,
        )

        # Should only generate specified levels
        assert len(output_files) == 2
        assert HintLevel.GENERAL_CLASS in output_files
        assert HintLevel.WITH_LINES in output_files
        assert HintLevel.SPECIFIC_TYPE not in output_files

    def test_output_directory_creation(self, temp_vuln_yaml):
        """Test that output directory is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "nested" / "hints"
            assert not output_dir.exists()

            generate_hints_for_benchmark(
                vuln_yaml_path=temp_vuln_yaml,
                output_dir=output_dir,
            )

            assert output_dir.exists()
            assert output_dir.is_dir()


class TestSarifValidation:
    """Tests to validate generated SARIF format."""

    def test_sarif_schema_compliance(self, sample_vuln_data):
        """Test that generated SARIF complies with basic schema requirements."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        for level in HintLevel:
            sarif_json = generator.generate(level)
            sarif = json.loads(sarif_json)

            # Required fields at root level
            assert "version" in sarif
            assert "runs" in sarif
            assert sarif["version"] == "2.1.0"

            # Required fields in run
            run = sarif["runs"][0]
            assert "tool" in run
            assert "results" in run

            # Required fields in tool
            tool = run["tool"]
            assert "driver" in tool
            driver = tool["driver"]
            assert "name" in driver
            assert "rules" in driver

            # Required fields in result
            for result in run["results"]:
                assert "ruleId" in result
                assert "message" in result
                assert "text" in result["message"]

    def test_physical_location_format(self, sample_vuln_data):
        """Test physical location format in SARIF."""
        vuln_info = VulnInfo(sample_vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        location = sarif["runs"][0]["results"][0]["locations"][0]
        physical = location["physicalLocation"]

        # Check physical location structure
        assert "artifactLocation" in physical
        assert "uri" in physical["artifactLocation"]
        assert "region" in physical

        # Check region structure
        region = physical["region"]
        assert "startLine" in region
        assert "endLine" in region


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_locations(self):
        """Test vulnerability with no location information."""
        vuln_data = {
            "id": "test",
            "name": "Test Vuln",
            "cwes": ["CWE-122"],
            "locations": [],
        }
        vuln_info = VulnInfo(vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        # Should generate valid SARIF even with no locations
        assert sarif["version"] == "2.1.0"
        result = sarif["runs"][0]["results"][0]
        # Locations should be None or empty
        assert result.get("locations") is None or len(result["locations"]) == 0

    def test_location_without_function_name(self):
        """Test location data without function name."""
        vuln_data = {
            "id": "test",
            "name": "Test Vuln",
            "cwes": ["CWE-122"],
            "locations": [
                {
                    "path_from_root": "test.c",
                    # No function_name field
                    "startLine": 10,
                    "endLine": 20,
                }
            ],
        }
        vuln_info = VulnInfo(vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_FUNCTION)
        sarif = json.loads(sarif_json)

        location = sarif["runs"][0]["results"][0]["locations"][0]
        # Should NOT have logicalLocations if function name is unknown
        assert "logicalLocations" not in location

    def test_single_location(self):
        """Test vulnerability with single location."""
        vuln_data = {
            "id": "test",
            "name": "Test Vuln",
            "cwes": ["CWE-122"],
            "locations": [
                {
                    "path_from_root": "single.c",
                    "function_name": "func",
                    "startLine": 10,
                    "endLine": 20,
                }
            ],
        }
        vuln_info = VulnInfo(vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        locations = sarif["runs"][0]["results"][0]["locations"]
        assert len(locations) == 1

    def test_location_without_columns(self):
        """Test location data without column information."""
        vuln_data = {
            "id": "test",
            "name": "Test Vuln",
            "cwes": ["CWE-122"],
            "locations": [
                {
                    "path_from_root": "test.c",
                    "function_name": "func",
                    "startLine": 10,
                    "endLine": 20,
                    # No column information
                }
            ],
        }
        vuln_info = VulnInfo(vuln_data)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        region = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
            "region"
        ]
        assert "startLine" in region
        assert "endLine" in region
        # Column info should not be present if not in source data
        assert "startColumn" not in region
        assert "endColumn" not in region


class TestRealWorldBenchmark:
    """Tests using actual benchmark data if available."""

    def test_libxml2_delta_03_if_exists(self):
        """Test with actual libxml2-delta-03 benchmark if it exists."""
        benchmark_path = Path("benchmarks/libxml2-delta-03/.aixcc/html/cpv_0/vuln.yaml")

        if not benchmark_path.exists():
            pytest.skip("libxml2-delta-03 benchmark not found")

        vuln_info = VulnInfo.from_yaml(benchmark_path)
        generator = SarifHintGenerator(vuln_info)

        # Generate all levels
        for level in HintLevel:
            sarif_json = generator.generate(level)
            sarif = json.loads(sarif_json)

            assert sarif["version"] == "2.1.0"
            assert len(sarif["runs"]) > 0

            # Verify level-appropriate content
            result = sarif["runs"][0]["results"][0]
            if level >= HintLevel.WITH_FUNCTION:
                assert "locations" in result or len(vuln_info.locations) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
