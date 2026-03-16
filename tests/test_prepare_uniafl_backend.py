"""Tests for Atlantis-backed UniAFL coverage backend preparation."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_uniafl_checkout(repo_root: Path) -> None:
    (repo_root / "oss-crs").mkdir(parents=True, exist_ok=True)
    (repo_root / "oss-crs" / "crs.yaml").write_text("name: atlantis\n")
    (repo_root / "bin").mkdir(parents=True, exist_ok=True)
    (repo_root / "bin" / "compile_target").write_text("#!/bin/bash\n")


def _write_uniafl_llvm_prep_script(repo_root: Path) -> Path:
    script = (
        repo_root
        / "libs"
        / "oss-fuzz"
        / "infra"
        / "base-images"
        / "multilang-clang"
        / "checkout_build_install_llvm.sh"
    )
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/bash\n"
        "OUR_LLVM_REVISION=llvmorg-18.1.8\n"
        "function clone_with_retries {\n"
        "  REPOSITORY=$1\n"
        "  LOCAL_PATH=$2\n"
        "    git clone $REPOSITORY $LOCAL_PATH\n"
        "}\n"
    )
    return script


def test_default_uniafl_root_uses_repo_third_party_checkout() -> None:
    import crsbench.prepare.uniafl_backend as backend

    expected = (
        Path(backend.__file__).resolve().parents[2]
        / "third_party"
        / "atlantis-multilang-given_fuzzer"
    )

    assert backend.default_uniafl_root() == expected


def test_default_uniafl_image_prefix_uses_ghcr_default() -> None:
    from crsbench.prepare.uniafl_backend import default_uniafl_image_prefix

    assert default_uniafl_image_prefix() == "ghcr.io/team-atlanta"


def test_prepare_image_refs_use_default_registry_prefix() -> None:
    from crsbench.prepare.uniafl_backend import prepare_image_refs

    assert prepare_image_refs(image_tag="stable")[:2] == (
        "ghcr.io/team-atlanta/multilang-given_fuzzer-clang:stable",
        "ghcr.io/team-atlanta/multilang-given_fuzzer-builder:stable",
    )


def test_default_uniafl_runtime_image_uses_jvm_specific_tag() -> None:
    from crsbench.prepare.uniafl_backend import default_uniafl_runtime_image

    assert (
        default_uniafl_runtime_image()
        == "ghcr.io/team-atlanta/multilang-given_fuzzer-crs:latest"
    )
    assert (
        default_uniafl_runtime_image("jvm")
        == "ghcr.io/team-atlanta/multilang-given_fuzzer-crs:latest"
    )


def test_prepare_uniafl_backend_requires_checkout(tmp_path: Path) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    with pytest.raises(FileNotFoundError):
        prepare_uniafl_backend(tmp_path / "missing")


def test_get_uniafl_prepare_readiness_reports_missing_state_and_images(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import get_uniafl_prepare_readiness

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)

    with patch(
        "crsbench.prepare.uniafl_backend._local_image_exists", return_value=False
    ):
        resolved_root, issues = get_uniafl_prepare_readiness(repo_root)

    assert resolved_root == repo_root.resolve()
    assert (
        f"missing prepare state: {repo_root.resolve() / '.crsbench-uniafl-prepare.json'}"
        in issues
    )
    assert (
        "missing local image: ghcr.io/team-atlanta/multilang-given_fuzzer-clang:latest"
        in issues
    )


def test_get_uniafl_prepare_readiness_reports_stale_state(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import (
        PREPARE_STATE_FILE,
        get_uniafl_prepare_readiness,
    )

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)
    (repo_root / PREPARE_STATE_FILE).write_text("{}")

    with (
        patch(
            "crsbench.prepare.uniafl_backend._prepare_state_matches", return_value=False
        ),
        patch("crsbench.prepare.uniafl_backend._local_image_exists", return_value=True),
    ):
        _resolved_root, issues = get_uniafl_prepare_readiness(repo_root)

    assert issues == [
        f"stale prepare state: {repo_root.resolve() / PREPARE_STATE_FILE}"
    ]


def test_prepare_state_roundtrip_ignores_state_file_in_git_status(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import (
        _prepare_state_matches,
        _write_prepare_state,
    )

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)

    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "codex@example.com"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Codex"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "init"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    control_dir = repo_root / ".crsbench-oss-crs-prepare"
    control_dir.mkdir()
    (control_dir / "compose.yaml").write_text("crs: []\n")

    _write_prepare_state(repo_root)

    assert _prepare_state_matches(repo_root) is True


def test_prepare_uniafl_backend_reuses_local_images_with_matching_state(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)

    with (
        patch("crsbench.prepare.uniafl_backend._local_image_exists", return_value=True),
        patch(
            "crsbench.prepare.uniafl_backend._prepare_state_matches",
            return_value=True,
        ),
        patch("crsbench.prepare.uniafl_backend.run_oss_crs_prepare") as mock_prepare,
        patch(
            "crsbench.prepare.uniafl_backend._write_prepare_state"
        ) as mock_write_state,
    ):
        assert prepare_uniafl_backend(repo_root) == 0

    mock_prepare.assert_not_called()
    mock_write_state.assert_not_called()


def test_prepare_uniafl_backend_pulls_ghcr_images_before_local_build(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)

    with (
        patch(
            "crsbench.prepare.uniafl_backend._prepare_state_matches",
            return_value=False,
        ),
        patch(
            "crsbench.prepare.uniafl_backend._pull_prepare_images", return_value=[]
        ) as mock_pull,
        patch(
            "crsbench.prepare.uniafl_backend._local_image_exists",
            side_effect=[False] * 6 + [True] * 6,
        ),
        patch("crsbench.prepare.uniafl_backend.run_oss_crs_prepare") as mock_prepare,
        patch(
            "crsbench.prepare.uniafl_backend._write_prepare_state"
        ) as mock_write_state,
    ):
        assert prepare_uniafl_backend(repo_root) == 0

    mock_pull.assert_called_once_with()
    mock_prepare.assert_not_called()
    mock_write_state.assert_called_once_with(repo_root.resolve())


def test_prepare_uniafl_backend_runs_oss_crs_prepare_with_generated_compose(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)
    _write_uniafl_llvm_prep_script(repo_root)

    generated: dict[str, Path] = {}

    def _fake_write_compose(
        *, compose_path: Path, uniafl_root: Path, **_kwargs
    ) -> Path:
        generated["compose_path"] = compose_path
        generated["uniafl_root"] = uniafl_root
        compose_path.parent.mkdir(parents=True, exist_ok=True)
        compose_path.write_text("crs: []\n")
        return compose_path

    with (
        patch(
            "crsbench.prepare.uniafl_backend._local_image_exists", return_value=False
        ),
        patch(
            "crsbench.prepare.uniafl_backend._pull_prepare_images",
            return_value=["ghcr pull failed"],
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.write_coverage_compose_yaml",
            side_effect=_fake_write_compose,
        ),
        patch(
            "crsbench.prepare.uniafl_backend.run_oss_crs_prepare",
            return_value=("prepared", "", 0),
        ) as mock_prepare,
        patch(
            "crsbench.prepare.uniafl_backend._write_prepare_state"
        ) as mock_write_state,
    ):
        assert prepare_uniafl_backend(repo_root) == 0

    control_root = repo_root / ".crsbench-oss-crs-prepare"
    assert generated["compose_path"] == control_root / "crs-compose.yaml"
    assert generated["uniafl_root"] == repo_root.resolve()
    mock_prepare.assert_called_once_with(
        control_root / "crs-compose.yaml",
        control_root / "oss-crs-workdir",
        oss_crs_cmd="oss-crs",
        timeout=3600,
    )
    mock_write_state.assert_called_once_with(repo_root.resolve())


def test_prepare_uniafl_backend_raises_when_oss_crs_prepare_fails(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)

    with (
        patch(
            "crsbench.prepare.uniafl_backend._local_image_exists", return_value=False
        ),
        patch(
            "crsbench.prepare.uniafl_backend._pull_prepare_images",
            return_value=["ghcr pull failed"],
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.write_coverage_compose_yaml",
            side_effect=lambda **kwargs: kwargs["compose_path"],
        ),
        patch(
            "crsbench.prepare.uniafl_backend.run_oss_crs_prepare",
            return_value=("", "boom", 7),
        ),
        patch("crsbench.prepare.uniafl_backend._write_prepare_state") as mock_state,
    ):
        with pytest.raises(RuntimeError, match="oss-crs prepare failed"):
            prepare_uniafl_backend(repo_root)

    mock_state.assert_not_called()


def test_prepare_uniafl_backend_leaves_llvm_checkout_script_unchanged(
    tmp_path: Path,
) -> None:
    from crsbench.prepare.uniafl_backend import prepare_uniafl_backend

    repo_root = tmp_path / "given_fuzzer"
    _write_uniafl_checkout(repo_root)
    script = _write_uniafl_llvm_prep_script(repo_root)
    original = script.read_text()

    def _assert_unmodified(*_args, **_kwargs):
        assert script.read_text() == original
        return "", "", 0

    with (
        patch(
            "crsbench.prepare.uniafl_backend._local_image_exists", return_value=False
        ),
        patch(
            "crsbench.evaluation.coverage.uniafl_runtime.write_coverage_compose_yaml",
            side_effect=lambda **kwargs: kwargs["compose_path"],
        ),
        patch(
            "crsbench.prepare.uniafl_backend.run_oss_crs_prepare",
            side_effect=_assert_unmodified,
        ),
        patch("crsbench.prepare.uniafl_backend._write_prepare_state"),
    ):
        prepare_uniafl_backend(repo_root)

    assert script.read_text() == original
