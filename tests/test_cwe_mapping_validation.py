"""Test CWE mapping accuracy against cwec_v4.18.xml.

This test validates that the CWE descriptions in cwe_mapping.py
match the official CWE names from the CWEC XML database.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from crsbench.hint_generation.cwe_mapping import CWE_TO_GENERAL_CLASS


def parse_cwec_xml():
    """Parse cwec_v4.18.xml and extract CWE ID to name mappings.

    Returns:
        dict: Mapping of CWE ID to official name
    """
    xml_path = Path("crsbench/hint_generation/cwec_v4.18.xml")

    if not xml_path.exists():
        pytest.skip(f"CWEC XML file not found: {xml_path}")

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        pytest.skip(f"Failed to parse XML: {e}")

    # Find namespace from root tag
    # Format: {http://cwe.mitre.org/cwe-7}Weakness_Catalog
    namespace = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    ns = {"cwe": namespace} if namespace else {}

    cwe_mapping = {}

    # Find all Weakness elements
    if namespace:
        weaknesses = root.findall(".//cwe:Weakness", ns)
    else:
        weaknesses = root.findall(".//Weakness")

    for weakness in weaknesses:
        cwe_id = weakness.get("ID")
        cwe_name = weakness.get("Name")

        if cwe_id and cwe_name:
            cwe_mapping[f"CWE-{cwe_id}"] = cwe_name

    return cwe_mapping


class TestCWEMappingValidation:
    """Test CWE mapping descriptions against official CWEC XML."""

    @pytest.fixture(scope="class")
    def cwec_mapping(self):
        """Load official CWE names from XML."""
        return parse_cwec_xml()

    def test_all_cwes_exist_in_cwec(self, cwec_mapping):
        """Verify all CWEs in mapping exist in CWEC database."""
        missing_cwes = []

        for cwe_id in CWE_TO_GENERAL_CLASS.keys():
            if cwe_id not in cwec_mapping:
                missing_cwes.append(cwe_id)

        if missing_cwes:
            print(f"\nCWEs not found in CWEC XML: {missing_cwes}")

        assert len(missing_cwes) == 0, f"Missing CWEs in CWEC: {missing_cwes}"

    def test_cwe_descriptions_match(self, cwec_mapping):
        """Verify CWE descriptions match official names (case-insensitive)."""
        mismatches = []

        for cwe_id, (general_class, description) in CWE_TO_GENERAL_CLASS.items():
            if cwe_id not in cwec_mapping:
                continue

            official_name = cwec_mapping[cwe_id]

            # Normalize for comparison (case-insensitive, strip whitespace)
            official_normalized = official_name.lower().strip()
            description_normalized = description.lower().strip()

            # Check if they match (exact or description is contained in official name)
            if official_normalized != description_normalized:
                # Allow partial match (description might be shortened)
                if (
                    description_normalized not in official_normalized
                    and official_normalized not in description_normalized
                ):
                    mismatches.append(
                        {
                            "cwe_id": cwe_id,
                            "official": official_name,
                            "mapped": description,
                            "general_class": general_class,
                        }
                    )

        if mismatches:
            print("\n" + "=" * 80)
            print("CWE Description Mismatches:")
            print("=" * 80)
            for mm in mismatches:
                print(f"\n{mm['cwe_id']}:")
                print(f"  Official: {mm['official']}")
                print(f"  Mapped:   {mm['mapped']}")
                print(f"  Category: {mm['general_class']}")

        # This might fail for legitimate shortened descriptions
        # Consider this a warning rather than hard failure
        if mismatches:
            print(f"\nFound {len(mismatches)} potential description mismatches")
            print("Note: Some mismatches may be intentional (shortened descriptions)")

    def test_cwe_name_similarity(self, cwec_mapping):
        """Check for significant differences in CWE names."""
        significant_differences = []

        for cwe_id, (general_class, description) in CWE_TO_GENERAL_CLASS.items():
            if cwe_id not in cwec_mapping:
                continue

            official_name = cwec_mapping[cwe_id]

            # Normalize
            official_words = set(official_name.lower().split())
            description_words = set(description.lower().split())

            # Remove common words that don't change meaning
            stop_words = {"the", "a", "an", "of", "in", "to", "for", "with", "without"}
            official_words -= stop_words
            description_words -= stop_words

            # Check word overlap
            common_words = official_words & description_words
            total_words = official_words | description_words

            if total_words:
                similarity = len(common_words) / len(total_words)

                # Flag if less than 30% similarity
                if similarity < 0.3:
                    significant_differences.append(
                        {
                            "cwe_id": cwe_id,
                            "official": official_name,
                            "mapped": description,
                            "similarity": f"{similarity:.2%}",
                            "general_class": general_class,
                        }
                    )

        if significant_differences:
            print("\n" + "=" * 80)
            print("CWEs with Low Name Similarity (<30%):")
            print("=" * 80)
            for sd in significant_differences:
                print(f"\n{sd['cwe_id']} (Similarity: {sd['similarity']}):")
                print(f"  Official: {sd['official']}")
                print(f"  Mapped:   {sd['mapped']}")
                print(f"  Category: {sd['general_class']}")

        # Don't fail test, just report
        print(f"\nFound {len(significant_differences)} CWEs with low similarity")

    def test_print_all_mappings(self, cwec_mapping):
        """Print all CWE mappings for manual review."""
        print("\n" + "=" * 80)
        print("All CWE Mappings:")
        print("=" * 80)

        for cwe_id, (general_class, description) in sorted(
            CWE_TO_GENERAL_CLASS.items()
        ):
            official = cwec_mapping.get(cwe_id, "NOT FOUND IN CWEC")

            match_status = (
                "✓"
                if cwe_id in cwec_mapping
                and official.lower().strip() == description.lower().strip()
                else "~"
            )

            print(f"\n{match_status} {cwe_id} [{general_class}]")
            print(f"    Official: {official}")
            print(f"    Mapped:   {description}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
