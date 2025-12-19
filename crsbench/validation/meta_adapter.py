"""MetaYamlAdapter for bridging benchmark config to variant building.

This adapter wraps BenchmarkConfig and provides methods for:
1. Accessing harness and POV information
2. Extracting CPV numbers for variant building
3. Determining benchmark mode (FULL/DELTA)
4. Generating variant names
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from crsbench.validation.schemas import BenchmarkConfig, HarnessFile, POV
from crsbench.validation.variant.models import BenchmarkMode, BuildTag

logger = logging.getLogger(__name__)


class MetaYamlAdapter:
    """Adapter for meta.yaml benchmark configuration.

    Provides a unified interface for accessing benchmark metadata and
    generating variant configurations for POV validation.

    Attributes:
        config: The parsed BenchmarkConfig
        benchmark_name: Name of the benchmark (e.g., "afc-curl-delta-01")
        lang: Programming language (e.g., "c", "jvm")
        main_repo: URL or path to the main repository
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        benchmark_name: str,
        lang: str,
        main_repo: str,
    ):
        """Initialize the adapter.

        Args:
            config: Parsed BenchmarkConfig from meta.yaml
            benchmark_name: Name of the benchmark
            lang: Programming language
            main_repo: Main repository URL/path
        """
        self.config = config
        self.benchmark_name = benchmark_name
        self.lang = lang
        self.main_repo = main_repo
        self._harness_map = {h.name: h for h in config.harness_files}

    @classmethod
    def from_meta_yaml(
        cls,
        meta_yaml_path: Path,
        benchmark_name: str,
        lang: str,
        main_repo: str,
    ) -> "MetaYamlAdapter":
        """Create adapter from meta.yaml file.

        Args:
            meta_yaml_path: Path to meta.yaml file
            benchmark_name: Name of the benchmark
            lang: Programming language
            main_repo: Main repository URL/path

        Returns:
            MetaYamlAdapter instance

        Raises:
            FileNotFoundError: If meta.yaml doesn't exist
            ValueError: If meta.yaml is invalid
        """
        if not meta_yaml_path.exists():
            raise FileNotFoundError(f"meta.yaml not found: {meta_yaml_path}")

        with open(meta_yaml_path, "r") as f:
            data = yaml.safe_load(f)

        try:
            config = BenchmarkConfig(**data)
        except Exception as e:
            raise ValueError(f"Invalid meta.yaml: {e}")

        return cls(config, benchmark_name, lang, main_repo)

    # =========================================================================
    # Harness and POV Access Methods
    # =========================================================================

    def get_harness(self, harness_name: str) -> Optional[HarnessFile]:
        """Get harness configuration by name.

        Args:
            harness_name: Name of the harness

        Returns:
            HarnessFile if found, None otherwise
        """
        return self._harness_map.get(harness_name)

    def get_harness_names(self) -> List[str]:
        """Get list of all harness names.

        Returns:
            List of harness names
        """
        return list(self._harness_map.keys())

    def get_all_povs(self, harness_name: str) -> List[Tuple[str, POV]]:
        """Get all POVs for a harness with their vulnerability keywords.

        Args:
            harness_name: Name of the harness

        Returns:
            List of (vuln_keyword, POV) tuples
        """
        harness = self.get_harness(harness_name)
        if not harness or not harness.vulns:
            return []

        result = []
        for vuln in harness.vulns:
            for pov in vuln.povs:
                result.append((vuln.vuln_keyword, pov))
        return result

    # =========================================================================
    # Variant Building Methods
    # =========================================================================

    def get_mode(self) -> BenchmarkMode:
        """Get the benchmark mode (FULL or DELTA).

        Returns:
            BenchmarkMode.DELTA if delta_mode is configured, else FULL
        """
        if self.config.delta_mode:
            return BenchmarkMode.DELTA
        return BenchmarkMode.FULL

    def get_base_commit(self) -> str:
        """Get the base commit hash.

        Returns:
            Base commit hash

        Raises:
            ValueError: If no mode is configured
        """
        if self.config.delta_mode:
            return self.config.delta_mode.base_commit
        if self.config.full_mode:
            return self.config.full_mode.base_commit
        raise ValueError("No mode configured (neither delta_mode nor full_mode)")

    def get_ref_commit(self) -> Optional[str]:
        """Get the reference commit hash (delta mode only).

        Returns:
            Reference commit hash if delta mode, None otherwise
        """
        if self.config.delta_mode:
            return self.config.delta_mode.ref_commit
        return None

    def get_cpv_numbers(self) -> List[int]:
        """Extract all CPV numbers from vulnerabilities.

        CPV numbers are extracted from vuln_keyword fields that match
        the pattern "cpv_N" where N is the CPV number.

        Returns:
            Sorted list of unique CPV numbers
        """
        cpvs = set()
        for harness in self.config.harness_files:
            if harness.vulns:
                for vuln in harness.vulns:
                    # vuln_keyword format: "cpv_0", "cpv_1", etc.
                    if vuln.vuln_keyword.startswith("cpv_"):
                        try:
                            cpv_num = int(vuln.vuln_keyword.split("_")[1])
                            cpvs.add(cpv_num)
                        except (IndexError, ValueError):
                            logger.warning(
                                f"Invalid CPV keyword format: {vuln.vuln_keyword}"
                            )
        return sorted(cpvs)

    def get_variant_name(
        self, build_tag: BuildTag, cpv_num: Optional[int] = None
    ) -> str:
        """Generate variant project name for a build tag.

        Args:
            build_tag: Type of variant (FULL_BASE, DELTA_REF, CPV, etc.)
            cpv_num: CPV number if build_tag is CPV

        Returns:
            Variant project name (e.g., "afc-curl-delta-01-cpv0")
        """
        if build_tag == BuildTag.CPV and cpv_num is not None:
            return f"{self.benchmark_name}-cpv{cpv_num}"
        return f"{self.benchmark_name}-{build_tag.value}"

    def get_patch_dir(self) -> Path:
        """Get the path to the patches directory.

        Returns:
            Path to patches directory (relative to benchmark project)
        """
        return Path(".aixcc") / "patches"

    def get_patch_exclude_list(self) -> List[str]:
        """Get list of files excluded from patches.

        Returns:
            List of file patterns that patches cannot modify
        """
        return self.config.patch_exclude_list or []
