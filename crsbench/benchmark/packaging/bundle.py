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

    Bundling differs based on benchmark type:
    - Delta mode benchmarks: tarball at ref_commit (vulnerable) + ref.diff hint
    - Full-only benchmarks: tarball at base_commit (vulnerable), no ref.diff

    Workflow:
    1. Validate benchmark structure
    2. Extract repo info from project.yaml and meta.yaml
    3. Determine source name from Dockerfile WORKDIR
    4. Clone repo, checkout vulnerable commit, create tarball
    5. Generate ref.diff if delta mode (base→ref diff as hint)
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

    # 2. Get benchmark info first (needed to determine source tarball name)
    info = get_benchmark_info(benchmark_path)
    if not info:
        raise ValueError(
            "Could not extract benchmark info. "
            "Ensure project.yaml has main_repo and meta.yaml has base_commit."
        )

    # 3. Determine source name from Dockerfile WORKDIR
    dockerfile = benchmark_path / "Dockerfile"
    source_name = get_expected_source_dir(dockerfile)
    if not source_name:
        # Fallback to benchmark name
        source_name = benchmark_path.name
        logger.warning(
            f"Could not determine source name from Dockerfile WORKDIR. "
            f"Using benchmark name: {source_name}"
        )

    # 4. Check for existing source tarball
    pkgs_dir = benchmark_path / "pkgs"
    source_tarball = pkgs_dir / f"{source_name}.tar.gz"
    aixcc_ref_diff = benchmark_path / ".aixcc" / "ref.diff"

    if source_tarball.exists():
        if not force:
            logger.warning(
                f"Source tarball already exists: {source_tarball}. "
                "Skipping. Use --force to overwrite."
            )
            return pkgs_dir
        logger.info(f"Overwriting existing source tarball: {source_tarball}")

    # 5. Extract commit info
    main_repo = str(info["main_repo"])
    base_commit = str(info["base_commit"])
    has_delta_mode = bool(info.get("has_delta_mode", False))
    ref_commit = str(info["ref_commit"]) if info.get("ref_commit") else None

    # Validate: delta mode requires ref_commit
    if has_delta_mode and not ref_commit:
        raise ValueError(
            "Delta mode benchmark requires ref_commit. "
            "Add ref_commit to delta_mode section in meta.yaml."
        )

    # 6. Log bundling info
    logger.info(f"Bundling {benchmark_path.name}:")
    logger.info(f"  Source: {main_repo}")
    logger.info(f"  Base commit: {base_commit[:8]}")
    if has_delta_mode:
        logger.info(f"  Ref commit: {ref_commit[:8] if ref_commit else 'N/A'}")
        logger.info("  Mode: delta (tarball at pre-vuln, ref.diff generated)")
    else:
        logger.info("  Mode: full-only (tarball at vulnerable state, no ref.diff)")
    logger.info(f"  Tarball name: {source_name}.tar.gz")

    # 7. Create tarball (and ref.diff if delta mode)
    pkgs_dir.mkdir(parents=True, exist_ok=True)

    tarball_path, ref_diff_path = create_source_tarball(
        repo_url=main_repo,
        base_commit=base_commit,
        source_name=source_name,
        output_dir=pkgs_dir,
        ref_commit=ref_commit,  # None for full-only benchmarks
    )

    # 8. Move ref.diff to .aixcc/ if generated (delta mode only)
    if ref_diff_path:
        # shutil.move behavior varies; explicitly remove dest to ensure overwrite
        if aixcc_ref_diff.exists():
            aixcc_ref_diff.unlink()
        shutil.move(str(ref_diff_path), str(aixcc_ref_diff))
        logger.info(f"  Moved ref.diff to: {aixcc_ref_diff}")

    # 9. Write pkg_refs.txt for provenance
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

    ref_commit_val = info.get("ref_commit")
    if not ref_commit_val or not isinstance(ref_commit_val, str):
        raise ValueError("Benchmark is not delta mode (no ref_commit in meta.yaml)")
    ref_commit: str = ref_commit_val

    # For prepare-delta, we only generate the diff, not the tarball
    import tempfile

    from crsbench.benchmark.packaging.tarball import _generate_ref_diff, _run_git

    main_repo_val = info["main_repo"]
    base_commit_val = info["base_commit"]
    # Type narrowing: these must be strings
    if not isinstance(main_repo_val, str) or not isinstance(base_commit_val, str):
        raise ValueError(
            "Invalid benchmark info: main_repo and base_commit must be strings"
        )
    main_repo: str = main_repo_val
    base_commit: str = base_commit_val
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
