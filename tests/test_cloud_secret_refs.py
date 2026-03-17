"""Tests for strict cloud secret reference parsing and resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_secret_text_returns_trimmed_literal() -> None:
    from crsbench.cloud.secret_refs import resolve_secret_text

    assert (
        resolve_secret_text(
            "  hf_test_token_abc123  ",
            field_path="cloud.providers.gce.instance_profiles.worker-n2d.hf_token",
        )
        == "hf_test_token_abc123"
    )


def test_resolve_secret_text_rejects_empty_literal() -> None:
    from crsbench.cloud.secret_refs import (
        CloudSecretReferenceError,
        resolve_secret_text,
    )

    with pytest.raises(CloudSecretReferenceError, match="hf_token"):
        resolve_secret_text(
            "   ",
            field_path="cloud.providers.gce.instance_profiles.worker-n2d.hf_token",
        )


def test_resolve_secret_text_resolves_env_reference() -> None:
    from crsbench.cloud.secret_refs import resolve_secret_text

    assert (
        resolve_secret_text(
            "os.environ/HF_TOKEN",
            field_path="cloud.providers.gce.instance_profiles.worker-n2d.hf_token",
            env={"HF_TOKEN": "hf_secret_value"},
        )
        == "hf_secret_value"
    )


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("os.environ/HF_TOKEN", "HF_TOKEN"),
        ("os.environ/EMPTY_TOKEN", "EMPTY_TOKEN"),
        ("os.environ/", "invalid"),
        ("prefix os.environ/HF_TOKEN suffix", "invalid"),
        ("os.environ/MY-KEY", "invalid"),
    ],
)
def test_resolve_secret_text_rejects_invalid_env_references(
    value: str,
    match: str,
) -> None:
    from crsbench.cloud.secret_refs import (
        CloudSecretReferenceError,
        resolve_secret_text,
    )

    env = {"EMPTY_TOKEN": "   "}
    with pytest.raises(CloudSecretReferenceError, match=match):
        resolve_secret_text(
            value,
            field_path="cloud.providers.gce.instance_profiles.worker-n2d.hf_token",
            env=env,
        )


def test_resolve_secret_text_reads_file_relative_to_cwd(tmp_path: Path) -> None:
    from crsbench.cloud.secret_refs import resolve_secret_text

    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    (secret_dir / "hf_token.txt").write_text("hf_file_token\n", encoding="utf-8")

    assert (
        resolve_secret_text(
            "file:.secrets/hf_token.txt",
            field_path="cloud.providers.gce.instance_profiles.worker-n2d.hf_token",
            cwd=tmp_path,
        )
        == "hf_file_token"
    )


def test_resolve_secret_path_reads_env_relative_to_cwd(tmp_path: Path) -> None:
    from crsbench.cloud.secret_refs import resolve_secret_path

    key_dir = tmp_path / ".crsbench-keys"
    key_dir.mkdir()
    key_path = key_dir / "crsbench-deploy"
    key_path.write_text("PRIVATE KEY", encoding="utf-8")

    resolved = resolve_secret_path(
        "os.environ/DEPLOY_KEY_PATH",
        field_path=(
            "cloud.providers.gce.instance_profiles.worker-n2d.github_deploy_key_file"
        ),
        env={"DEPLOY_KEY_PATH": ".crsbench-keys/crsbench-deploy"},
        cwd=tmp_path,
    )

    assert resolved == str(key_path)


@pytest.mark.parametrize(
    "value",
    [
        "file:.crsbench-keys/missing",
        ".crsbench-keys/missing",
    ],
)
def test_resolve_secret_path_reports_missing_file_with_field_context(
    tmp_path: Path,
    value: str,
) -> None:
    from crsbench.cloud.secret_refs import (
        CloudSecretReferenceError,
        resolve_secret_path,
    )

    with pytest.raises(
        CloudSecretReferenceError,
        match="github_deploy_key_file",
    ):
        resolve_secret_path(
            value,
            field_path=(
                "cloud.providers.gce.instance_profiles.worker-n2d.github_deploy_key_file"
            ),
            cwd=tmp_path,
        )
