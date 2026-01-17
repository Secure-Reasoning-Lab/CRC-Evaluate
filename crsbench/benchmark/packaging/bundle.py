"""High-level bundling interface for benchmarks.

Combines WORKDIR parsing, tarball creation, and validation into a single
workflow for creating distributable benchmark packages.
"""

import shutil
from pathlib import Path

from crsbench.benchmark.packaging.tarball import create_source_tarball
from crsbench.benchmark.packaging.validate import (
    get_benchmark_info,
    validate_benchmark,
)
from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def bundle_benchmark(
    benchmark_path: Path,
    *,
    force: bool = False,
) -> Path:
    """Bundle benchmark by creating pkgs/ with source tarball.

    Workflow:
    1. Validate benchmark structure
    2. Extract repo info from project.yaml and meta.yaml
    3. Determine source name from Dockerfile WORKDIR
    4. Clone repo, checkout base_commit, create tarball
    5. Generate ref.diff if delta mode (ref_commit exists)
    6. Write pkg_refs.txt for provenance

    Args:
        benchmark_path: Path to benchmark directory
        force: If True, overwrite existing pkgs/

    Returns:
        Path to created pkgs/ directory

    Raises:
        ValueError: If benchmark is invalid or missing required info
        RuntimeError: If bundling fails
    """
    benchmark_path = Path(benchmark_path).resolve()

    # 1. Validate benchmark
    result = validate_benchmark(benchmark_path)
    if not result.valid:
        raise ValueError(f"Invalid benchmark: {result}")

    # 2. Check for existing pkgs/
    pkgs_dir = benchmark_path / "pkgs"
    if pkgs_dir.exists():
        if not force:
            raise ValueError(
                f"pkgs/ already exists at {pkgs_dir}. Use --force to overwrite."
            )
        logger.warning(f"Removing existing pkgs/: {pkgs_dir}")
        shutil.rmtree(pkgs_dir)

    # 3. Get benchmark info
    info = get_benchmark_info(benchmark_path)
    if not info:
        raise ValueError(
            "Could not extract benchmark info. "
            "Ensure project.yaml has main_repo and meta.yaml has base_commit."
        )

    main_repo = info["main_repo"]
    base_commit = info["base_commit"]
    ref_commit = info.get("ref_commit")

    # 4. Determine source name from Dockerfile WORKDIR
    dockerfile = benchmark_path / "Dockerfile"
    source_name = get_expected_source_dir(dockerfile)
    if not source_name:
        # Fallback to benchmark name
        source_name = benchmark_path.name
        logger.warning(
            f"Could not determine source name from Dockerfile WORKDIR. "
            f"Using benchmark name: {source_name}"
        )

    logger.info(f"Bundling {benchmark_path.name}:")
    logger.info(f"  Source: {main_repo}")
    logger.info(f"  Base commit: {base_commit[:8]}")
    if ref_commit:
        logger.info(f"  Ref commit: {ref_commit[:8]} (delta mode)")
    logger.info(f"  Tarball name: {source_name}.tar.gz")

    # 5. Create tarball
    pkgs_dir.mkdir(parents=True, exist_ok=True)

    tarball_path, ref_diff_path = create_source_tarball(
        repo_url=main_repo,
        base_commit=base_commit,
        source_name=source_name,
        output_dir=pkgs_dir,
        ref_commit=ref_commit,
    )

    # 6. Move ref.diff to .aixcc/ if generated
    if ref_diff_path:
        aixcc_ref_diff = benchmark_path / ".aixcc" / "ref.diff"
        shutil.move(str(ref_diff_path), str(aixcc_ref_diff))
        logger.info(f"  Moved ref.diff to: {aixcc_ref_diff}")

    # 7. Write pkg_refs.txt for provenance
    pkg_refs_path = pkgs_dir / "pkg_refs.txt"
    pkg_refs_path.write_text(f"{main_repo}@{base_commit}\n")
    logger.info(f"  Wrote provenance: {pkg_refs_path}")

    logger.info(f"Successfully bundled: {benchmark_path.name}")
    return pkgs_dir


def prepare_delta(benchmark_path: Path) -> Path:
    """Generate ref.diff for a delta-mode benchmark.

    This is a lighter-weight operation than full bundling - it only
    generates the ref.diff file without creating a new tarball.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Path to generated ref.diff

    Raises:
        ValueError: If benchmark is not delta mode or missing required info
    """
    benchmark_path = Path(benchmark_path).resolve()

    info = get_benchmark_info(benchmark_path)
    if not info:
        raise ValueError("Could not extract benchmark info")

    ref_commit = info.get("ref_commit")
    if not ref_commit:
        raise ValueError("Benchmark is not delta mode (no ref_commit in meta.yaml)")

    # For prepare-delta, we only generate the diff, not the tarball
    import tempfile

    from crsbench.benchmark.packaging.tarball import _generate_ref_diff, _run_git

    main_repo = info["main_repo"]
    base_commit = info["base_commit"]
    output_dir = benchmark_path / ".aixcc"

    logger.info(f"Generating ref.diff for {benchmark_path.name}")
    logger.info(f"  Base: {base_commit[:8]}")
    logger.info(f"  Ref: {ref_commit[:8]}")

    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)

        # Clone repo
        _run_git(["clone", main_repo, "repo"], cwd=work_dir)
        repo_dir = work_dir / "repo"

        # Generate diff
        ref_diff_path = _generate_ref_diff(
            repo_dir=repo_dir,
            base_commit=base_commit,
            ref_commit=ref_commit,
            work_dir=work_dir,
            output_dir=output_dir,
        )

    logger.info(f"Generated: {ref_diff_path}")
    return ref_diff_path
