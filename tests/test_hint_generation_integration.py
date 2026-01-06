"""Integration tests for hint generation with real benchmark data.

This test suite verifies hint generation using actual benchmark projects
to ensure end-to-end functionality.
"""

import json
import tempfile
from pathlib import Path

import pytest
from crsbench.hint_generation.sarif_generator_simple import (
    HintLevel,
    SarifHintGenerator,
    VulnInfo,
    generate_hints_for_benchmark,
)


def find_benchmark_vuln_yamls() -> list[Path]:
    """Find all vuln.yaml files in benchmarks directory."""
    benchmarks_dir = Path("benchmarks")
    if not benchmarks_dir.exists():
        return []
    return list(benchmarks_dir.glob("*/.aixcc/html/*/vuln.yaml"))


@pytest.fixture
def benchmark_vuln_yamls() -> list[Path]:
    """Fixture providing all available benchmark vuln.yaml files."""
    vuln_yamls = find_benchmark_vuln_yamls()
    if not vuln_yamls:
        pytest.skip("No benchmark vuln.yaml files found")
    return vuln_yamls


@pytest.fixture
def libxml2_delta_03_vuln_yaml() -> Path:
    """Fixture for afc-libxml2-delta-03 benchmark."""
    vuln_yaml = Path("benchmarks/afc-libxml2-delta-03/.aixcc/html/cpv_0/vuln.yaml")
    if not vuln_yaml.exists():
        pytest.skip("afc-libxml2-delta-03 benchmark not found")
    return vuln_yaml


@pytest.fixture
def libxml2_delta_01_vuln_yaml() -> Path:
    """Fixture for afc-libxml2-delta-01 benchmark."""
    vuln_yaml = Path("benchmarks/afc-libxml2-delta-01/.aixcc/html/cpv_0/vuln.yaml")
    if not vuln_yaml.exists():
        pytest.skip("afc-libxml2-delta-01 benchmark not found")
    return vuln_yaml


class TestRealBenchmarkHintGeneration:
    """Integration tests with real benchmark data."""

    def test_libxml2_delta_03_all_levels(self, libxml2_delta_03_vuln_yaml):
        """Test hint generation for afc-libxml2-delta-03 at all levels."""
        vuln_info = VulnInfo.from_yaml(libxml2_delta_03_vuln_yaml)
        generator = SarifHintGenerator(vuln_info)

        # Verify loaded data
        assert vuln_info.id == "cpv_0"
        assert "CWE-122" in vuln_info.cwes
        assert len(vuln_info.locations) > 0

        # Generate hints for all levels
        for level in HintLevel:
            sarif_json = generator.generate(level)
            sarif = json.loads(sarif_json)

            # Validate SARIF structure
            assert sarif["version"] == "2.1.0"
            assert len(sarif["runs"]) == 1

            run = sarif["runs"][0]
            assert len(run["results"]) > 0

            # Check level-specific content
            result = run["results"][0]
            message = result["message"]["text"]

            if level == HintLevel.GENERAL_CLASS:
                assert "Memory Safety Issue" in message
                assert "locations" not in result

            elif level == HintLevel.SPECIFIC_TYPE:
                assert "CWE-122" in message
                assert "locations" not in result

            elif level == HintLevel.WITH_FUNCTION:
                assert "CWE-122" in message
                assert "function" in message.lower() or len(vuln_info.locations) == 0
                if len(vuln_info.locations) > 0:
                    assert "locations" in result

            elif level == HintLevel.WITH_LINES:
                assert "CWE-122" in message
                if len(vuln_info.locations) > 0:
                    assert "locations" in result
                    location = result["locations"][0]
                    physical = location["physicalLocation"]
                    assert "region" in physical
                    assert "startLine" in physical["region"]

    def test_libxml2_delta_01_all_levels(self, libxml2_delta_01_vuln_yaml):
        """Test hint generation for afc-libxml2-delta-01 at all levels."""
        vuln_info = VulnInfo.from_yaml(libxml2_delta_01_vuln_yaml)
        generator = SarifHintGenerator(vuln_info)

        # Verify loaded data
        assert vuln_info.id == "cpv_0"
        assert "CWE-122" in vuln_info.cwes

        # Generate and validate all levels
        for level in HintLevel:
            sarif_json = generator.generate(level)
            sarif = json.loads(sarif_json)
            assert sarif["version"] == "2.1.0"

    def test_all_benchmarks_generate_valid_sarif(self, benchmark_vuln_yamls):
        """Test that all benchmarks generate valid SARIF at all levels."""
        for vuln_yaml_path in benchmark_vuln_yamls:
            benchmark_name = vuln_yaml_path.parts[-5]  # Extract benchmark name
            print(f"\nTesting benchmark: {benchmark_name}")

            vuln_info = VulnInfo.from_yaml(vuln_yaml_path)
            generator = SarifHintGenerator(vuln_info)

            # Test all levels
            for level in HintLevel:
                try:
                    sarif_json = generator.generate(level)
                    sarif = json.loads(sarif_json)

                    # Basic validation
                    assert sarif["version"] == "2.1.0"
                    assert "runs" in sarif
                    assert len(sarif["runs"]) > 0

                    run = sarif["runs"][0]
                    assert "tool" in run
                    assert "results" in run

                except Exception as e:
                    pytest.fail(
                        f"Failed to generate level {level} for {benchmark_name}: {e}"
                    )


class TestBenchmarkFileGeneration:
    """Test generating hint files for benchmarks."""

    def test_generate_hint_files_libxml2_delta_03(self, libxml2_delta_03_vuln_yaml):
        """Test generating hint files for afc-libxml2-delta-03."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "hints"

            output_files = generate_hints_for_benchmark(
                vuln_yaml_path=libxml2_delta_03_vuln_yaml,
                output_dir=output_dir,
            )

            # Verify all files were created
            assert len(output_files) == 5
            for level, file_path in output_files.items():
                assert file_path.exists()
                assert file_path.name == f"level_{level}.sarif"

                # Verify file content
                with open(file_path, "r") as f:
                    sarif = json.load(f)
                    assert sarif["version"] == "2.1.0"

    def test_generate_specific_levels_only(self, libxml2_delta_03_vuln_yaml):
        """Test generating only specific hint levels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "hints"
            levels = [HintLevel.GENERAL_CLASS, HintLevel.WITH_LINES]

            output_files = generate_hints_for_benchmark(
                vuln_yaml_path=libxml2_delta_03_vuln_yaml,
                output_dir=output_dir,
                levels=levels,
            )

            # Should only have 2 files
            assert len(output_files) == 2
            assert HintLevel.GENERAL_CLASS in output_files
            assert HintLevel.WITH_LINES in output_files

            # Files should exist and be valid
            for level, file_path in output_files.items():
                assert file_path.exists()
                with open(file_path, "r") as f:
                    sarif = json.load(f)
                    assert sarif["version"] == "2.1.0"


class TestHintContentValidation:
    """Validate the content of generated hints matches expectations."""

    def test_level_1_contains_only_general_class(self, libxml2_delta_03_vuln_yaml):
        """Verify Level 1 hint contains only general vulnerability class."""
        vuln_info = VulnInfo.from_yaml(libxml2_delta_03_vuln_yaml)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.GENERAL_CLASS)
        sarif = json.loads(sarif_json)

        result = sarif["runs"][0]["results"][0]
        message = result["message"]["text"]

        # Should mention general class
        assert "Memory Safety Issue" in message

        # Should NOT mention specific CWE
        assert "CWE-122" not in message

        # Should NOT have locations
        assert "locations" not in result

    def test_level_4_contains_full_location_info(self, libxml2_delta_03_vuln_yaml):
        """Verify Level 4 hint contains complete location information."""
        vuln_info = VulnInfo.from_yaml(libxml2_delta_03_vuln_yaml)
        generator = SarifHintGenerator(vuln_info)

        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        result = sarif["runs"][0]["results"][0]

        # Should have specific CWE
        assert "CWE-122" in result["message"]["text"]

        # Should have locations with line numbers
        if len(vuln_info.locations) > 0:
            assert "locations" in result
            location = result["locations"][0]
            physical = location["physicalLocation"]

            # Should have artifact location
            assert "artifactLocation" in physical
            assert "uri" in physical["artifactLocation"]

            # Should have region with line numbers
            assert "region" in physical
            region = physical["region"]
            assert "startLine" in region
            assert "endLine" in region

    def test_multiple_locations_handled_correctly(self):
        """Test that multiple vulnerability locations are handled properly."""
        # Find a benchmark with multiple locations
        vuln_yamls = find_benchmark_vuln_yamls()
        if not vuln_yamls:
            pytest.skip("No benchmarks found")

        # Look for vuln with multiple locations
        multi_location_vuln = None
        for vuln_yaml in vuln_yamls:
            vuln_info = VulnInfo.from_yaml(vuln_yaml)
            if len(vuln_info.locations) > 1:
                multi_location_vuln = vuln_info
                break

        if not multi_location_vuln:
            pytest.skip("No vulnerabilities with multiple locations found")

        generator = SarifHintGenerator(multi_location_vuln)
        sarif_json = generator.generate(HintLevel.WITH_LINES)
        sarif = json.loads(sarif_json)

        result = sarif["runs"][0]["results"][0]

        # Should have multiple locations
        assert "locations" in result
        assert len(result["locations"]) == len(multi_location_vuln.locations)


class TestProgressiveLevelInformation:
    """Test that each level progressively adds more information."""

    def test_information_increases_with_level(self, libxml2_delta_03_vuln_yaml):
        """Verify that higher levels contain more information."""
        vuln_info = VulnInfo.from_yaml(libxml2_delta_03_vuln_yaml)
        generator = SarifHintGenerator(vuln_info)

        sarifs = {}
        for level in HintLevel:
            sarif_json = generator.generate(level)
            sarifs[level] = json.loads(sarif_json)

        # Level 1: Only general class
        level1_result = sarifs[HintLevel.GENERAL_CLASS]["runs"][0]["results"][0]
        assert "locations" not in level1_result

        # Level 2: Adds specific CWE
        level2_result = sarifs[HintLevel.SPECIFIC_TYPE]["runs"][0]["results"][0]
        assert "CWE" in level2_result["message"]["text"]
        assert "locations" not in level2_result

        # Level 3: Adds function-level location
        if len(vuln_info.locations) > 0:
            level3_result = sarifs[HintLevel.WITH_FUNCTION]["runs"][0]["results"][0]
            assert "locations" in level3_result

        # Level 4: Adds line-level location
        if len(vuln_info.locations) > 0:
            level4_result = sarifs[HintLevel.WITH_LINES]["runs"][0]["results"][0]
            assert "locations" in level4_result
            location = level4_result["locations"][0]
            assert "region" in location["physicalLocation"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
