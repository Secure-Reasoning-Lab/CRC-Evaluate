"""Tests for CRC submission validation and local registry generation."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
import yaml
from crsbench.run_experiment import parse_arguments
from crsbench.submission.manifest import (
    SubmissionError,
    load_submission,
    register_submission,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_crs(root: Path, relative_path: str, *, name: str, crs_type: str) -> Path:
    crs_root = root / relative_path
    config_dir = crs_root / "oss-crs"
    config_dir.mkdir(parents=True)
    (config_dir / "crs.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "type": [crs_type],
                "version": "1.0.0",
                "crs_run_phase": {"main": {"dockerfile": "oss-crs/runner.Dockerfile"}},
                "supported_target": {
                    "mode": ["full", "delta"],
                    "language": ["c", "c++"],
                    "sanitizer": ["address"],
                    "architecture": ["x86_64"],
                    "fuzzing_engine": ["libfuzzer"],
                },
                "required_llms": ["test-model"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return crs_root


def _write_manifest(
    root: Path,
    *,
    finder_path: str = "crs/finder",
    patcher_path: str = "crs/patcher",
) -> None:
    (root / "submission.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "submission": {"name": "Example Submission"},
                "crs": {
                    "finder": {"path": finder_path},
                    "patcher": {"path": patcher_path},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def submission_root(tmp_path: Path) -> Path:
    _write_manifest(tmp_path)
    _write_crs(tmp_path, "crs/finder", name="example-finder", crs_type="bug-finding")
    _write_crs(tmp_path, "crs/patcher", name="example-patcher", crs_type="bug-fixing")
    return tmp_path


def test_load_submission_validates_selected_crses(submission_root: Path) -> None:
    submission = load_submission(submission_root)

    assert submission.name == "Example Submission"
    assert submission.finder.name == "example-finder"
    assert submission.finder.crs_type == "bug-finding"
    assert submission.finder.required_llms == ("test-model",)
    assert submission.patcher.name == "example-patcher"
    assert submission.patcher.crs_type == "bug-fixing"


def test_load_submission_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-finder"
    _write_crs(
        outside.parent,
        outside.name,
        name="outside-finder",
        crs_type="bug-finding",
    )
    _write_manifest(tmp_path, finder_path="../outside-finder")
    _write_crs(tmp_path, "crs/patcher", name="patcher", crs_type="bug-fixing")

    with pytest.raises(SubmissionError, match="must not contain"):
        load_submission(tmp_path)


def test_load_submission_rejects_role_type_mismatch(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_crs(tmp_path, "crs/finder", name="finder", crs_type="bug-fixing")
    _write_crs(tmp_path, "crs/patcher", name="patcher", crs_type="bug-fixing")

    with pytest.raises(SubmissionError, match="must declare type 'bug-finding'"):
        load_submission(tmp_path)


def test_load_submission_rejects_invalid_oss_crs_config(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    finder_root = _write_crs(
        tmp_path,
        "crs/finder",
        name="finder",
        crs_type="bug-finding",
    )
    _write_crs(tmp_path, "crs/patcher", name="patcher", crs_type="bug-fixing")
    finder_config = finder_root / "oss-crs/crs.yaml"
    data = yaml.safe_load(finder_config.read_text())
    del data["crs_run_phase"]
    finder_config.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(SubmissionError, match="Invalid OSS-CRS configuration"):
        load_submission(tmp_path)


def test_register_submission_writes_namespaced_registry_entries(
    submission_root: Path,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "registry"

    registered = register_submission(
        submission_root,
        team_id="team-001",
        registry_dir=registry_dir,
    )

    finder = yaml.safe_load(registered.finder_registry_path.read_text())
    patcher = yaml.safe_load(registered.patcher_registry_path.read_text())
    assert finder == {
        "name": "team-001-finder",
        "type": ["bug-finding"],
        "source": {"local_path": str(submission_root / "crs/finder")},
    }
    assert patcher == {
        "name": "team-001-patcher",
        "type": ["bug-fixing"],
        "source": {"local_path": str(submission_root / "crs/patcher")},
    }


def test_register_submission_refuses_to_replace_existing_entries(
    submission_root: Path,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "registry"
    register_submission(
        submission_root,
        team_id="team-001",
        registry_dir=registry_dir,
    )

    with pytest.raises(SubmissionError, match="already exist"):
        register_submission(
            submission_root,
            team_id="team-001",
            registry_dir=registry_dir,
        )


def test_register_submission_force_replaces_existing_entries(
    submission_root: Path,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "registry"
    registered = register_submission(
        submission_root,
        team_id="team-001",
        registry_dir=registry_dir,
    )
    registered.finder_registry_path.write_text("invalid: true\n", encoding="utf-8")

    register_submission(
        submission_root,
        team_id="team-001",
        registry_dir=registry_dir,
        force=True,
    )

    finder = yaml.safe_load(registered.finder_registry_path.read_text())
    assert finder["name"] == "team-001-finder"
    assert finder["type"] == ["bug-finding"]


def test_submission_cli_parses_register_arguments(
    submission_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_dir = tmp_path / "registry"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crsbench",
            "submission",
            "register",
            str(submission_root),
            "--team-id",
            "team-001",
            "--registry-dir",
            str(registry_dir),
        ],
    )

    args = parse_arguments()

    assert args.command == "submission"
    assert args.submission_action == "register"
    assert args.team_id == "team-001"
    assert args.registry_dir == registry_dir
