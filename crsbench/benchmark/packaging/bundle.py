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
    validate_tarball_naming,
)
from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _update_pkg_refs(
    pkg_refs_path: Path,
    main_repo: str,
    vulnerable_commit: str,
) -> None:
    """Update pkg_refs.txt, appending our entry if not present.

    Preserves all original entries for full provenance. Only adds our
    main_repo@commit entry if the URL isn't already in the file.

    Args:
        pkg_refs_path: Path to pkg_refs.txt
        main_repo: Main repository URL
        vulnerable_commit: Commit hash for the main source
    """
    new_entry = f"{main_repo}@{vulnerable_commit}"
    existing_content = ""

    if pkg_refs_path.exists():
        existing_content = pkg_refs_path.read_text()
        # Check if our repo URL already exists (ignore commit hash)
        if main_repo in existing_content:
            return  # Already present, don't duplicate

    # Append our entry
    with pkg_refs_path.open("a") as f:
        # Add newline if file doesn't end with one
        if existing_content and not existing_content.endswith("\n"):
            f.write("\n")
        f.write(f"{new_entry}\n")


def bundle_benchmark(
    benchmark_path: Path,
    *,
    force: bool = False,
) -> Path:
    """Bundle benchmark by creating pkgs/ with source tarball.

    Bundling differs based on benchmark type:
    - Delta mode benchmarks: tarball at ref_commit (vulnerable) + ref.diff hint
    - Full-only benchmarks: tarball at base_commit (vulnerable), no ref.diff
    Tarball always contains the VULNERABLE state so patches can fix it:
    - Delta mode: tarball at ref_commit (vulnerable), base_commit is benign
    - Full mode: tarball at base_commit (vulnerable)

    Workflow:
    1. Validate benchmark structure
    2. Extract repo info from project.yaml and meta.yaml
    3. Determine source name from Dockerfile WORKDIR
    4. Clone repo, checkout vulnerable commit, create tarball
    5. Generate ref.diff if delta mode (diff from base to ref)
    6. Write pkg_refs.txt for provenance (records vulnerable commit)

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

    # 4b. Check for tarball naming mismatch (VAL-03)
    # Reject if pkgs/ has misnamed tarballs (unless --force)
    if pkgs_dir.exists():
        naming_result = validate_tarball_naming(benchmark_path)
        if not naming_result.valid:
            if not force:
                raise ValueError(
                    f"Tarball naming mismatch (VAL-03): {'; '.join(naming_result.errors)}. "
                    "Fix the naming or use --force to override."
                )
            logger.warning(
                f"Tarball naming mismatch detected, will be corrected: "
                f"{'; '.join(naming_result.errors)}"
            )

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

    # 6. Determine vulnerable commit (tarball always contains vulnerable state)
    # Delta mode: ref_commit is vulnerable, base_commit is benign
    # Full mode: base_commit is vulnerable
    vulnerable_commit = ref_commit if has_delta_mode else base_commit
    assert vulnerable_commit is not None  # Guaranteed by validation above

    # 7. Log bundling info
    benchmark_name = benchmark_path.name
    mode_str = "delta" if has_delta_mode else "full"
    logger.info(
        f"[{benchmark_name}] Starting bundle: {vulnerable_commit[:8]} ({mode_str})"
    )

    # 8. Create tarball (and ref.diff if delta mode)
    pkgs_dir.mkdir(parents=True, exist_ok=True)

    tarball_path, ref_diff_path = create_source_tarball(
        repo_url=main_repo,
        base_commit=base_commit,
        source_name=source_name,
        output_dir=pkgs_dir,
        ref_commit=ref_commit,  # None for full-only benchmarks
        log_prefix=benchmark_name,
    )

    # 9. Move ref.diff to .aixcc/ if generated (delta mode only)
    if ref_diff_path:
        # shutil.move behavior varies; explicitly remove dest to ensure overwrite
        if aixcc_ref_diff.exists():
            aixcc_ref_diff.unlink()
        shutil.move(str(ref_diff_path), str(aixcc_ref_diff))

    # 10. Update pkg_refs.txt for provenance (preserves other package refs)
    pkg_refs_path = pkgs_dir / "pkg_refs.txt"
    _update_pkg_refs(pkg_refs_path, main_repo, vulnerable_commit)

    logger.info(f"[{benchmark_name}] Bundle complete")
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
