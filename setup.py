"""Setup script for CRSBench with submodule commit capture.

Uses setuptools-scm for main repo version, plus custom hook for submodules.
"""

import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


def get_git_commit(repo_path: Path) -> str:
    """Get git commit hash for a repository.

    Args:
        repo_path: Path to git repository

    Returns:
        Commit hash or "unknown"
    """
    try:
        if not repo_path.exists():
            return "unknown"

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


class BuildPyCommand(build_py):
    """Custom build_py that captures submodule commits."""

    def run(self):
        """Capture submodule commits before building."""
        project_root = Path(__file__).parent.resolve()

        # Capture submodule commits from default locations (if they exist)
        oss_crs_commit = get_git_commit(project_root / "oss-crs")
        oss_fuzz_commit = get_git_commit(project_root / "oss-fuzz")

        # Write to a separate file so setuptools-scm doesn't interfere
        submodule_file = project_root / "crsbench" / "_submodule_commits.py"
        content = f'''"""Submodule commits captured at build time.

This file is auto-generated during pip install.
"""

# Build-time captured commits (from default submodule locations)
OSS_CRS_COMMIT = "{oss_crs_commit}"
OSS_FUZZ_COMMIT = "{oss_fuzz_commit}"
'''
        submodule_file.write_text(content)
        print(f"Captured submodule commits:")
        print(f"  oss-crs:  {oss_crs_commit[:8] if oss_crs_commit != 'unknown' else 'unknown'}")
        print(f"  oss-fuzz: {oss_fuzz_commit[:8] if oss_fuzz_commit != 'unknown' else 'unknown'}")

        super().run()


setup(cmdclass={"build_py": BuildPyCommand})
