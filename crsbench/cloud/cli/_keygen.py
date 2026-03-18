"""keygen sub-action: generate an SSH ed25519 deploy key pair for fleet access."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def run_keygen(args: argparse.Namespace) -> int:
    """Generate an SSH ed25519 deploy key pair.

    Writes the private key and public key to the output directory, prints the
    public key to stdout so the operator can paste it into GitHub as a deploy
    key, and prints the private key path for use in the fleet config.

    Returns 0 on success, 1 on error.
    """
    output_dir = Path(args.output_dir).expanduser().resolve()
    name: str = args.name

    try:
        output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create output directory {}: {}", output_dir, exc)
        return 1

    key_path = output_dir / name
    pub_path = Path(str(key_path) + ".pub")

    if key_path.exists() or pub_path.exists():
        if not args.force:
            logger.warning(
                "Key already exists at %s — skipping generation. "
                "Use --force to overwrite.",
                key_path,
            )
            if pub_path.exists():
                sys.stdout.write(pub_path.read_text(encoding="utf-8").strip() + "\n")
            return 0
        # Remove existing files before regenerating
        for p in (key_path, pub_path):
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("Cannot remove existing key file {}: {}", p, exc)
                return 1

    try:
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(key_path),
                "-N",
                "",
                "-C",
                "crsbench-deploy-key",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("ssh-keygen failed: {}", exc.stderr.decode(errors="replace"))
        return 1
    except FileNotFoundError:
        logger.error("ssh-keygen not found — install OpenSSH to use this command")
        return 1

    # Ensure private key is mode 600
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.warning("Could not chmod private key {}: {}", key_path, exc)

    pub_key = pub_path.read_text(encoding="utf-8").strip()

    _write = sys.stdout.write
    _write(pub_key + "\n")
    _write("\n")
    _write(
        "Add the public key above as a deploy key on your GitHub repository "
        "(Settings > Deploy keys > Add deploy key).\n"
    )
    _write("\n")
    _write("Private key path (set github_deploy_key_file in your fleet config):\n")
    _write(f"  {key_path}\n")

    return 0
