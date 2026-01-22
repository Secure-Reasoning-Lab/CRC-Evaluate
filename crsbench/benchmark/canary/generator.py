"""Canary string generation and injection utilities."""

import fnmatch
import json
import re
import uuid
from json import JSONDecodeError
from pathlib import Path
from typing import Optional
from uuid import UUID

from pydantic import ValidationError

from crsbench.benchmark.canary.models import (
    CANARY_WARNING,
    CanaryRegistry,
    InjectionResult,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Default registry filename (resolved relative to cwd at runtime)
DEFAULT_REGISTRY_FILENAME = "canary-registry.json"


def _get_default_registry_path() -> Path:
    """Get default registry path based on current working directory.

    Returns:
        Path to registry file in current working directory
    """
    return Path.cwd() / DEFAULT_REGISTRY_FILENAME


def load_registry(registry_path: Optional[Path] = None) -> CanaryRegistry:
    """Load canary registry from file or create new one.

    Args:
        registry_path: Path to registry file (default: ./canary-registry.json)

    Returns:
        CanaryRegistry instance

    Raises:
        PermissionError: If registry file cannot be read due to permissions
    """
    path = registry_path or _get_default_registry_path()

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return CanaryRegistry.model_validate(data)
        except JSONDecodeError as e:
            logger.error(f"Registry file is corrupted (invalid JSON): {path}")
            raise ValueError(f"Corrupted registry file: {e}") from e
        except ValidationError as e:
            logger.error(f"Registry file has invalid structure: {path}")
            raise ValueError(f"Invalid registry structure: {e}") from e
        except PermissionError:
            logger.error(f"Permission denied reading registry: {path}")
            raise

    return CanaryRegistry()


def save_registry(
    registry: CanaryRegistry, registry_path: Optional[Path] = None
) -> None:
    """Save canary registry to file.

    Args:
        registry: CanaryRegistry to save
        registry_path: Path to registry file
    """
    path = registry_path or _get_default_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    logger.info(f"Saved canary registry to {path}")


def generate_canary_block(canary_uuid: UUID) -> str:
    """Generate the canary comment block (BIG-bench style with CRSBench prefix).

    Args:
        canary_uuid: UUID for this canary

    Returns:
        Comment block string to prepend to files
    """
    return f"# {CANARY_WARNING}\n# canary GUID {canary_uuid}\n"


def inject_canary_into_file(
    file_path: Path,
    canary_uuid: UUID,
) -> bool:
    """Inject canary comment block into a file.

    Prepends canary as comments at the top of the file.

    Args:
        file_path: Path to the file
        canary_uuid: UUID to inject

    Returns:
        True if canary was injected, False if already present or file not found
    """
    if not file_path.exists():
        logger.warning(f"File not found: {file_path}")
        return False

    content = file_path.read_text(encoding="utf-8")

    # Check if canary already exists
    if "canary GUID" in content:
        logger.debug(f"Canary already present in {file_path}")
        return False

    # Prepend canary block
    canary_block = generate_canary_block(canary_uuid)
    new_content = canary_block + "\n" + content
    file_path.write_text(new_content, encoding="utf-8")

    logger.debug(f"Injected canary into {file_path}")
    return True


def inject_canary_into_benchmark(
    benchmark_path: Path,
    canary_uuid: UUID,
) -> int:
    """Inject canary into all relevant files in a benchmark.

    Injects into:
    - .aixcc/meta.yaml
    - .aixcc/**/vuln.yaml
    - .aixcc/ref.diff (ground truth patch)
    - .aixcc/**/*.patch (any patch files)
    - .aixcc/**/*.diff (any diff files)

    Args:
        benchmark_path: Path to the benchmark directory
        canary_uuid: UUID to inject

    Returns:
        Number of files injected
    """
    aixcc_dir = benchmark_path / ".aixcc"

    if not aixcc_dir.exists():
        logger.warning(f"No .aixcc directory in {benchmark_path}")
        return 0

    injected = 0

    # Inject into meta.yaml
    meta_yaml = aixcc_dir / "meta.yaml"
    if meta_yaml.exists() and inject_canary_into_file(meta_yaml, canary_uuid):
        injected += 1

    # Inject into all vuln.yaml files
    for vuln_yaml in aixcc_dir.rglob("vuln.yaml"):
        if inject_canary_into_file(vuln_yaml, canary_uuid):
            injected += 1

    # Inject into ref.diff (ground truth patch)
    ref_diff = aixcc_dir / "ref.diff"
    if ref_diff.exists() and inject_canary_into_file(ref_diff, canary_uuid):
        injected += 1

    # Inject into all .patch files
    for patch_file in aixcc_dir.rglob("*.patch"):
        if inject_canary_into_file(patch_file, canary_uuid):
            injected += 1

    # Inject into all .diff files (excluding ref.diff already handled)
    for diff_file in aixcc_dir.rglob("*.diff"):
        if diff_file != ref_diff and inject_canary_into_file(diff_file, canary_uuid):
            injected += 1

    return injected


def inject_canaries_by_prefix(
    benchmarks_dir: Path,
    prefix_filter: str,
    canary_uuid: Optional[UUID] = None,
    registry_path: Optional[Path] = None,
    *,
    force: bool = False,
) -> InjectionResult:
    """Inject canaries into all benchmarks matching a prefix.

    All matching benchmarks get the SAME UUID (per-prefix, not per-benchmark).

    Args:
        benchmarks_dir: Directory containing benchmarks
        prefix_filter: Glob pattern to filter benchmarks (e.g., "atlanta-*")
        canary_uuid: Optional UUID to use (generates new if not provided)
        registry_path: Path to canary registry file
        force: If True, re-inject even if canary exists

    Returns:
        InjectionResult with summary
    """
    # Load or create registry
    registry = load_registry(registry_path)

    # Get or generate UUID for this prefix
    if canary_uuid is None:
        canary_uuid = registry.get_or_create_uuid(prefix_filter)

    result = InjectionResult(
        prefix=prefix_filter,
        canary_uuid=canary_uuid,
    )

    # Find matching benchmarks
    for benchmark_path in sorted(benchmarks_dir.iterdir()):
        if not benchmark_path.is_dir():
            continue
        if benchmark_path.name.startswith("."):
            continue
        if not (benchmark_path / ".aixcc").exists():
            continue
        if not fnmatch.fnmatch(benchmark_path.name, prefix_filter):
            continue

        # Check if already has canary (unless force)
        meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
        if meta_yaml.exists() and not force:
            content = meta_yaml.read_text(encoding="utf-8")
            if "canary GUID" in content:
                result.skipped_count += 1
                continue

        # Inject canary
        injected = inject_canary_into_benchmark(benchmark_path, canary_uuid)
        if injected > 0:
            result.injected_count += injected
            result.benchmarks.append(benchmark_path.name)

    # Save registry
    save_registry(registry, registry_path)

    return result


def extract_canary_from_file(file_path: Path) -> Optional[UUID]:
    """Extract canary UUID from a file.

    Args:
        file_path: Path to file

    Returns:
        UUID if found, None otherwise
    """
    if not file_path.exists():
        return None

    content = file_path.read_text(encoding="utf-8")

    # Match: canary GUID <uuid> or CRSBench: canary GUID <uuid>
    match = re.search(
        r"(?:CRSBench:\s*)?canary GUID ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        content,
        re.IGNORECASE,
    )

    if match:
        try:
            return uuid.UUID(match.group(1))
        except ValueError:
            pass

    return None


def extract_canary_from_benchmark(benchmark_path: Path) -> Optional[UUID]:
    """Extract canary UUID from a benchmark's meta.yaml.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        UUID if found, None otherwise
    """
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
    return extract_canary_from_file(meta_yaml)
