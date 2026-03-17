#!/usr/bin/env python3
"""Render file-backed metadata directories for local cloud startup rehearsal."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--experiment-config", type=Path, required=True)
    parser.add_argument("--repo-mount-path", default="/src/CRSBench")
    parser.add_argument("--source-repo-path", type=Path, required=True)
    parser.add_argument("--git-ref", default=None)
    parser.add_argument("--worker-count", type=int, default=2)
    return parser.parse_args()


def detect_git_ref(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "main"
    return result.stdout.strip() or "main"


def main() -> int:
    args = parse_args()
    layout = build_local_rehearsal_layout(
        output_dir=args.output_dir,
        experiment_config_path=args.experiment_config,
        repo_mount_path=args.repo_mount_path,
        worker_count=args.worker_count,
        git_ref=args.git_ref or detect_git_ref(args.source_repo_path),
    )
    print(f"Rendered local rehearsal metadata under {layout.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
