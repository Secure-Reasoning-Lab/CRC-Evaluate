from pathlib import Path
from unittest.mock import patch

from crsbench.evaluation.replay.projects import (
    ensure_project_link,
    sync_managed_projects_root,
)


def test_ensure_project_link_repoints_helper_checkout(tmp_path: Path) -> None:
    oss_fuzz_path = tmp_path / "oss-fuzz"
    latest_projects = tmp_path / "latest-projects"
    (oss_fuzz_path / "projects").mkdir(parents=True)
    (latest_projects / "curl").mkdir(parents=True)

    link_path = ensure_project_link(oss_fuzz_path, latest_projects, "curl")

    assert link_path.is_symlink()
    assert link_path.resolve() == (latest_projects / "curl").resolve()


def test_sync_managed_projects_root_bootstraps_sparse_checkout(tmp_path: Path) -> None:
    managed_checkout = tmp_path / "managed-oss-fuzz-projects"

    with patch("crsbench.evaluation.replay.projects.run_git") as mock_git:
        sync_managed_projects_root(managed_checkout)

    clone_cmd = mock_git.call_args_list[0].args[0]
    sparse_cmd = mock_git.call_args_list[1].args[0]
    assert clone_cmd[:5] == ["clone", "--filter=blob:none", "--depth", "1", "--sparse"]
    assert sparse_cmd[-2:] == ["set", "projects"]
