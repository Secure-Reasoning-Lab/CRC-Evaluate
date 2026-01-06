"""Tests for CRS helper utilities."""

import tempfile
import unittest
from pathlib import Path

from crsbench.utils.crs_helper import get_all_crs_registry_names, get_crs_registry_name


class TestCRSHelper(unittest.TestCase):
    """Test CRS helper functions."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.crs_configs_dir = Path(self.temp_dir) / "configs"
        self.crs_configs_dir.mkdir(parents=True)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_get_all_crs_registry_names_single(self):
        """Test getting CRS names from config with single CRS."""
        # Create test config directory with single CRS
        config_dir = self.crs_configs_dir / "single-crs"
        config_dir.mkdir()

        config_resource_path = config_dir / "config-resource.yaml"
        config_resource_path.write_text(
            """
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

crs:
  crs-libfuzzer:
    workers:
      - local
"""
        )

        names = get_all_crs_registry_names("single-crs", self.crs_configs_dir)

        self.assertEqual(names, ["crs-libfuzzer"])

    def test_get_all_crs_registry_names_multiple(self):
        """Test getting CRS names from config with multiple CRS entries."""
        # Create test config directory with multiple CRS
        config_dir = self.crs_configs_dir / "multi-crs"
        config_dir.mkdir()

        config_resource_path = config_dir / "config-resource.yaml"
        config_resource_path.write_text(
            """
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

crs:
  atlantis-multilang-dind:
    workers:
      - local
  atlantis-multilang:
    workers:
      - local
  some-other-crs:
    workers:
      - local
"""
        )

        names = get_all_crs_registry_names("multi-crs", self.crs_configs_dir)

        # Should return all three CRS names
        self.assertEqual(len(names), 3)
        self.assertIn("atlantis-multilang-dind", names)
        self.assertIn("atlantis-multilang", names)
        self.assertIn("some-other-crs", names)

    def test_get_all_crs_registry_names_missing_file(self):
        """Test error when config file doesn't exist."""
        with self.assertRaises(FileNotFoundError) as cm:
            get_all_crs_registry_names("nonexistent", self.crs_configs_dir)

        self.assertIn("not found", str(cm.exception))

    def test_get_all_crs_registry_names_missing_crs_section(self):
        """Test error when config has no CRS section."""
        config_dir = self.crs_configs_dir / "no-crs"
        config_dir.mkdir()

        config_resource_path = config_dir / "config-resource.yaml"
        config_resource_path.write_text(
            """
workers:
  local:
    cpuset: "0-15"
"""
        )

        with self.assertRaises(ValueError) as cm:
            get_all_crs_registry_names("no-crs", self.crs_configs_dir)

        self.assertIn("crs", str(cm.exception).lower())

    def test_get_crs_registry_name_returns_first(self):
        """Test that get_crs_registry_name returns first CRS from multi-CRS config."""
        # Create test config directory with multiple CRS
        config_dir = self.crs_configs_dir / "multi-crs-2"
        config_dir.mkdir()

        config_resource_path = config_dir / "config-resource.yaml"
        config_resource_path.write_text(
            """
crs:
  first-crs:
    workers: [local]
  second-crs:
    workers: [local]
  third-crs:
    workers: [local]
"""
        )

        # get_crs_registry_name should return the first one
        name = get_crs_registry_name("multi-crs-2", self.crs_configs_dir)

        self.assertEqual(name, "first-crs")


if __name__ == "__main__":
    unittest.main()
