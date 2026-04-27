from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from crsbench.utils.repo_manager import run_git

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_OSS_FUZZ_REPO = "https://github.com/google/oss-fuzz.git"


def default_managed_projects_checkout(repo_root: Path) -> Path:
    return repo_root / ".crsbench-repos" / "oss-fuzz-projects"


def resolve_projects_root(
    explicit_projects_root: Path | None,
    *,
    sync_projects: bool,
    repo_root: Path,
) -> Path:
    if explicit_projects_root is not None:
        return explicit_projects_root.resolve()

    managed_checkout = default_managed_projects_checkout(repo_root)
    managed_projects_root = managed_checkout / "projects"
    if managed_projects_root.exists() and not sync_projects:
        return managed_projects_root.resolve()
    if sync_projects:
        return sync_managed_projects_root(managed_checkout)

    msg = (
        "Provide --projects-root or --sync-projects so replay uses a latest "
        "OSS-Fuzz projects mirror"
    )
    raise RuntimeError(msg)


def sync_managed_projects_root(
    checkout_path: Path,
    *,
    repo_url: str = DEFAULT_OSS_FUZZ_REPO,
) -> Path:
    checkout_path = checkout_path.resolve()
    checkout_path.parent.mkdir(parents=True, exist_ok=True)

    if not (checkout_path / ".git").exists():
        run_git(
            [
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                repo_url,
                str(checkout_path),
            ],
            capture_output=True,
            text=True,
        )
    else:
        run_git(
            ["-C", str(checkout_path), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )

    run_git(
        ["-C", str(checkout_path), "sparse-checkout", "set", "projects"],
        capture_output=True,
        text=True,
    )
    return (checkout_path / "projects").resolve()


def ensure_project_link(
    oss_fuzz_path: Path,
    projects_root: Path,
    project_name: str,
) -> Path:
    source = (projects_root / project_name).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Latest OSS-Fuzz project missing: {source}")

    target = oss_fuzz_path / "projects" / project_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.resolve() == source:
            return target
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    target.symlink_to(source, target_is_directory=True)
    return target
