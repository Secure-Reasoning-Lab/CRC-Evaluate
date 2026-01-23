"""MetaYamlAdapter for bridging benchmark config to variant building.

This adapter wraps BenchmarkConfig and provides methods for:
1. Accessing harness and POV information
2. Extracting CPV numbers for variant building
3. Determining benchmark mode (FULL/DELTA)
4. Generating variant names
"""

from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import POV, BenchmarkConfig, HarnessFile

logger = get_logger(__name__)


def _load_project_config(project_yaml: Path) -> tuple[str, str, Optional[str], bool]:
    """Load project configuration from project.yaml.

    Extracts language, main_repo, repo_name, and inc_build from a benchmark's
    project.yaml file, with sensible defaults for missing values.

    Args:
        project_yaml: Path to project.yaml file.

    Returns:
        Tuple of (language, main_repo, repo_name, inc_build).
        - language defaults to "c"
        - main_repo defaults to ""
        - repo_name defaults to None
        - inc_build defaults to False
    """
    lang = "c"
    main_repo = ""
    repo_name = None
    inc_build = False

    if project_yaml.exists():
        try:
            with project_yaml.open() as f:
                project_data = yaml.safe_load(f)
            if project_data:
                lang = project_data.get("language", "c")
                main_repo = project_data.get("main_repo", "")
                repo_name = project_data.get("repo_name")
                inc_build = project_data.get("inc_build", True)
        except Exception as e:
            logger.warning(f"Failed to load project.yaml: {e}")

    return lang, main_repo, repo_name, inc_build


class MetaYamlAdapter:
    """Adapter for meta.yaml benchmark configuration.

    Provides a unified interface for accessing benchmark metadata and
    generating variant configurations for POV validation.

    Attributes:
        config: The parsed BenchmarkConfig
        benchmark_name: Name of the benchmark (e.g., "afc-curl-delta-01")
        lang: Programming language (e.g., "c", "jvm")
        main_repo: URL or path to the main repository
        repo_name: Optional repository name (overrides derivation from URL)
        benchmark_path: Full path to the benchmark directory
        inc_build: Whether to use incremental builds
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        benchmark_name: str,
        lang: str,
        main_repo: str,
        benchmark_path: Optional[Path] = None,
        repo_name: Optional[str] = None,
        *,
        inc_build: bool = False,
    ):
        """Initialize the adapter.

        Args:
            config: Parsed BenchmarkConfig from meta.yaml
            benchmark_name: Name of the benchmark
            lang: Programming language
            main_repo: Main repository URL/path
            benchmark_path: Full path to the benchmark directory
            repo_name: Optional repository name (overrides derivation from URL)
            inc_build: Whether to use incremental builds (default: False)
        """
        self.config = config
        self.benchmark_name = benchmark_name
        self.lang = lang
        self.main_repo = main_repo
        self.repo_name = repo_name
        self.benchmark_path = benchmark_path
        self.inc_build = inc_build
        self._harness_map = {h.name: h for h in config.harness_files}

    @classmethod
    def from_meta_yaml(
        cls,
        meta_yaml_path: Path,
        benchmark_name: str,
        lang: str,
        main_repo: str,
        benchmark_path: Optional[Path] = None,
        repo_name: Optional[str] = None,
        *,
        inc_build: bool = False,
    ) -> "MetaYamlAdapter":
        """Create adapter from meta.yaml file.

        Args:
            meta_yaml_path: Path to meta.yaml file
            benchmark_name: Name of the benchmark
            lang: Programming language
            main_repo: Main repository URL/path
            benchmark_path: Full path to the benchmark directory
            repo_name: Optional repository name (overrides derivation from URL)
            inc_build: Whether to use incremental builds (default: False)

        Returns:
            MetaYamlAdapter instance

        Raises:
            FileNotFoundError: If meta.yaml doesn't exist
            ValueError: If meta.yaml is invalid
        """
        if not meta_yaml_path.exists():
            raise FileNotFoundError(f"meta.yaml not found: {meta_yaml_path}")

        with meta_yaml_path.open() as f:
            data = yaml.safe_load(f)

        try:
            config = BenchmarkConfig(**data)
        except Exception as e:
            raise ValueError(f"Invalid meta.yaml: {e}") from e

        # Derive benchmark_path from meta_yaml_path if not provided
        if benchmark_path is None:
            benchmark_path = (
                meta_yaml_path.parent.parent
            )  # .aixcc/meta.yaml -> benchmark_dir

        return cls(
            config,
            benchmark_name,
            lang,
            main_repo,
            benchmark_path,
            repo_name,
            inc_build=inc_build,
        )

    @classmethod
    def from_benchmark_path(cls, benchmark_path: Path) -> Optional["MetaYamlAdapter"]:
        """Create adapter from benchmark directory path.

        Convenience method that loads both meta.yaml and project.yaml from
        a benchmark directory. This is the recommended entry point for loading
        a MetaYamlAdapter when you have just the benchmark path.

        Used by:
        - VerificationEngine (POV)
        - PatchVerificationEngine
        - CoverageEngine

        Args:
            benchmark_path: Path to benchmark directory containing .aixcc/meta.yaml.

        Returns:
            MetaYamlAdapter instance, or None if loading fails.

        Example:
            adapter = MetaYamlAdapter.from_benchmark_path(Path("benchmarks/my-project"))
            if adapter:
                harnesses = adapter.get_harness_names()
        """
        benchmark_path = Path(benchmark_path)
        meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
        project_yaml = benchmark_path / "project.yaml"

        if not meta_yaml.exists():
            logger.error(f"meta.yaml not found: {meta_yaml}")
            return None

        benchmark_name = benchmark_path.name

        # Load project configuration
        lang, main_repo, repo_name, inc_build = _load_project_config(project_yaml)

        try:
            return cls.from_meta_yaml(
                meta_yaml_path=meta_yaml,
                benchmark_name=benchmark_name,
                lang=lang,
                main_repo=main_repo,
                repo_name=repo_name,
                benchmark_path=benchmark_path,
                inc_build=inc_build,
            )
        except Exception as e:
            logger.error(f"Failed to load adapter: {e}")
            return None

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

    def get_pov_path(
        self, harness_name: str, vuln_keyword: str, pov_id: str
    ) -> Optional[Path]:
        """Get the path to a POV blob file.

        CRSBench structure: .aixcc/{harness}/{vuln_keyword}/blobs/{pov_id}.blob

        Args:
            harness_name: Name of the harness
            vuln_keyword: Vulnerability keyword (e.g., "cpv_0")
            pov_id: POV identifier (e.g., "pov_0")

        Returns:
            Full path to POV blob, or None if benchmark_path not set
        """
        if not self.benchmark_path:
            return None
        return (
            self.benchmark_path
            / ".aixcc"
            / harness_name
            / vuln_keyword
            / "blobs"
            / f"{pov_id}.blob"
        )

    def get_all_pov_paths(self) -> List[Tuple[str, str, Path]]:
        """Get paths to all POV blobs in the benchmark.

        Discovers POV blobs by scanning the directory structure:
        .aixcc/{harness}/{cpv_X}/blobs/pov_*.blob

        This ensures ALL blob files are tested, not just those listed in meta.yaml.

        Returns:
            List of (harness_name, vuln_keyword, pov_path) tuples sorted by
            (harness, cpv number, pov number)
        """
        if not self.benchmark_path:
            return []

        result = []
        aixcc_dir = self.benchmark_path / ".aixcc"

        if not aixcc_dir.exists():
            return []

        # Iterate through harness directories
        for harness_dir in sorted(aixcc_dir.iterdir()):
            if not harness_dir.is_dir():
                continue

            harness_name = harness_dir.name

            # Skip non-harness entries (meta.yaml, etc.)
            if harness_name.endswith(".yaml") or harness_name.startswith("."):
                continue

            # Iterate through CPV directories
            for cpv_dir in sorted(harness_dir.iterdir()):
                if not cpv_dir.is_dir() or not cpv_dir.name.startswith("cpv_"):
                    continue

                vuln_keyword = cpv_dir.name
                blobs_dir = cpv_dir / "blobs"

                if not blobs_dir.exists():
                    continue

                # Discover all POV blob files
                pov_files = list(blobs_dir.glob("pov_*.blob"))

                # Sort by POV number (pov_0, pov_1, pov_2, ...)
                def extract_pov_num(path: Path) -> int:
                    try:
                        return int(path.stem.split("_")[1])
                    except (IndexError, ValueError):
                        return 999

                pov_files.sort(key=extract_pov_num)

                for pov_path in pov_files:
                    result.append((harness_name, vuln_keyword, pov_path))

        return result

    def get_patch_path(self, harness_name: str, vuln_keyword: str) -> Optional[Path]:
        """Get the path to the first patch file for a vulnerability.

        CRSBench structure: .aixcc/{harness}/{vuln_keyword}/patches/patch_*.diff

        Args:
            harness_name: Name of the harness
            vuln_keyword: Vulnerability keyword (e.g., "cpv_0")

        Returns:
            Path to the first patch file if found, None otherwise
        """
        if not self.benchmark_path:
            return None

        patches_dir = (
            self.benchmark_path / ".aixcc" / harness_name / vuln_keyword / "patches"
        )

        if not patches_dir.exists():
            return None

        patch_files = list(patches_dir.glob("patch_*.diff"))
        if patch_files:
            return patch_files[0]

        return None

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

        Only delta mode has ref_commit. Full mode doesn't need it because
        the base_commit IS the vulnerable state.

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
        self, variant_type: VariantType, cpv_num: Optional[int] = None
    ) -> str:
        """Generate variant project name for a variant type.

        Naming convention:
        - Base/ref variants include mode in type: {benchmark}-deltabase, {benchmark}-fullbase
        - Shared variants need mode prefix: {benchmark}-delta-allpatched, {benchmark}-delta-cpv0

        Args:
            variant_type: Type of variant (FULL_BASE, DELTA_REF, CPV, etc.)
            cpv_num: CPV number if variant_type is CPV

        Returns:
            Variant project name (e.g., "afc-curl-delta-01-delta-cpv0")
        """
        # Base and ref variants already have mode in type name
        if variant_type in (
            VariantType.FULL_BASE,
            VariantType.DELTA_BASE,
            VariantType.DELTA_REF,
        ):
            return f"{self.benchmark_name}-{variant_type.value}"

        # Shared variants need mode prefix to avoid conflicts
        mode = self.get_mode()
        mode_prefix = mode.value

        if variant_type == VariantType.CPV and cpv_num is not None:
            return f"{self.benchmark_name}-{mode_prefix}-cpv{cpv_num}"

        return f"{self.benchmark_name}-{mode_prefix}-{variant_type.value}"

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
