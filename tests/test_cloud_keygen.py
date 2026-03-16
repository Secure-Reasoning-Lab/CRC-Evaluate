"""Tests for the crsbench cloud keygen CLI subcommand."""

from __future__ import annotations

import argparse
import stat
from pathlib import Path
from unittest.mock import patch

from crsbench.cloud.cli._keygen import run_keygen


def _make_args(output_dir: str, name: str = "crsbench-deploy", force: bool = False):
    args = argparse.Namespace()
    args.output_dir = output_dir
    args.name = name
    args.force = force
    return args


def _fake_keygen(output_dir: Path, name: str = "crsbench-deploy") -> None:
    """Write dummy key files as ssh-keygen would."""
    key_path = output_dir / name
    pub_path = Path(str(key_path) + ".pub")
    key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n", encoding="utf-8")
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    pub_path.write_text("ssh-ed25519 AAAAB3Nza crsbench-deploy-key\n", encoding="utf-8")


def test_keygen_generates_key_pair(tmp_path):
    """run_keygen should invoke ssh-keygen with ed25519 args and return 0."""
    output_dir = tmp_path / "keys"
    args = _make_args(str(output_dir))

    def fake_run(_cmd, **_kwargs):
        _fake_keygen(output_dir)
        return None

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        result = run_keygen(args)

    assert result == 0
    assert mock_run.called
    call_args = mock_run.call_args[0][0]
    assert "ssh-keygen" in call_args
    assert "-t" in call_args
    assert "ed25519" in call_args
    assert "-N" in call_args
    assert "" in call_args
    key_path = output_dir / "crsbench-deploy"
    assert key_path.exists()
    assert (output_dir / "crsbench-deploy.pub").exists()


def test_keygen_respects_custom_name(tmp_path):
    """run_keygen should use the provided --name for key file names."""
    output_dir = tmp_path / "keys"
    args = _make_args(str(output_dir), name="my-key")

    def fake_run(_cmd, **_kwargs):
        _fake_keygen(output_dir, name="my-key")
        return None

    with patch("subprocess.run", side_effect=fake_run):
        result = run_keygen(args)

    assert result == 0
    assert (output_dir / "my-key").exists()
    assert (output_dir / "my-key.pub").exists()


def test_keygen_skips_if_key_exists(tmp_path):
    """run_keygen should exit 0 without invoking ssh-keygen if key already exists."""
    output_dir = tmp_path / "keys"
    output_dir.mkdir(mode=0o700)
    _fake_keygen(output_dir)

    args = _make_args(str(output_dir))

    with patch("subprocess.run") as mock_run:
        result = run_keygen(args)

    assert result == 0
    mock_run.assert_not_called()


def test_keygen_force_overwrites_existing_key(tmp_path):
    """--force should delete existing keys and regenerate."""
    output_dir = tmp_path / "keys"
    output_dir.mkdir(mode=0o700)
    _fake_keygen(output_dir)

    old_content = (output_dir / "crsbench-deploy.pub").read_text(encoding="utf-8")
    args = _make_args(str(output_dir), force=True)

    new_pub_content = "ssh-ed25519 BBBBB crsbench-deploy-key\n"

    def fake_run(_cmd, **_kwargs):
        key_path = output_dir / "crsbench-deploy"
        pub_path = Path(str(key_path) + ".pub")
        key_path.write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nNEW\n", encoding="utf-8"
        )
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        pub_path.write_text(new_pub_content, encoding="utf-8")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        result = run_keygen(args)

    assert result == 0
    mock_run.assert_called_once()
    written = (output_dir / "crsbench-deploy.pub").read_text(encoding="utf-8")
    assert written != old_content
    assert "BBBBB" in written


def test_keygen_returns_1_on_ssh_keygen_failure(tmp_path):
    """run_keygen should return 1 when ssh-keygen exits non-zero."""
    import subprocess

    output_dir = tmp_path / "keys"
    args = _make_args(str(output_dir))

    with patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "ssh-keygen", stderr=b"error"),
    ):
        result = run_keygen(args)

    assert result == 1
