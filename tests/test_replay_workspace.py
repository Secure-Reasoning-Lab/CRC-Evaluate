from pathlib import Path

from crsbench.evaluation.replay.workspace import (
    ensure_cached_oss_fuzz_workspace,
    resolve_cache_projects_root,
)


def _write_seed_checkout(seed_root: Path) -> None:
    (seed_root / "infra").mkdir(parents=True)
    (seed_root / "projects").mkdir(parents=True)
    (seed_root / "docker").mkdir(parents=True)
    (seed_root / "infra" / "helper.py").write_text("# helper\n", encoding="utf-8")
    (seed_root / "docker" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")


def test_ensure_cached_oss_fuzz_workspace_bootstraps_once_and_reuses(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache-root"
    seed_root = tmp_path / "seed-oss-fuzz"
    _write_seed_checkout(seed_root)

    cached_root = ensure_cached_oss_fuzz_workspace(
        cache_root,
        seed_oss_fuzz_root=seed_root,
    )
    assert cached_root == (cache_root / "oss-fuzz-helper").resolve()
    assert (cached_root / "infra" / "helper.py").read_text(
        encoding="utf-8"
    ) == "# helper\n"
    assert not (cached_root / "build").exists()

    (cached_root / "infra" / "helper.py").write_text("# modified\n", encoding="utf-8")
    reused_root = ensure_cached_oss_fuzz_workspace(
        cache_root,
        seed_oss_fuzz_root=seed_root,
    )
    assert reused_root == cached_root
    assert (cached_root / "infra" / "helper.py").read_text(
        encoding="utf-8"
    ) == "# modified\n"


def test_resolve_cache_projects_root_syncs_on_first_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_root = tmp_path / "cache-root"
    expected = cache_root / "oss-fuzz-projects" / "projects"

    def _fake_sync(checkout_path: Path) -> Path:
        assert checkout_path == (cache_root / "oss-fuzz-projects").resolve()
        expected.mkdir(parents=True)
        return expected.resolve()

    monkeypatch.setattr(
        "crsbench.evaluation.replay.workspace.sync_managed_projects_root",
        _fake_sync,
    )

    projects_root = resolve_cache_projects_root(cache_root, sync_projects=False)

    assert projects_root == expected.resolve()
    assert projects_root.exists()


def test_resolve_cache_projects_root_reuses_existing_checkout_without_sync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_root = tmp_path / "cache-root"
    projects_root = cache_root / "oss-fuzz-projects" / "projects"
    projects_root.mkdir(parents=True)

    def _unexpected_sync(checkout_path: Path) -> Path:
        raise AssertionError(f"unexpected sync for {checkout_path}")

    monkeypatch.setattr(
        "crsbench.evaluation.replay.workspace.sync_managed_projects_root",
        _unexpected_sync,
    )

    resolved = resolve_cache_projects_root(cache_root, sync_projects=False)

    assert resolved == projects_root.resolve()
