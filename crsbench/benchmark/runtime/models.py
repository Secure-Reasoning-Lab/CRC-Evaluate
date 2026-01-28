"""Data models for benchmark runtime loading."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BenchmarkSource:
    """Represents loaded benchmark source for CRS execution.

    Attributes:
        path: Path to source directory where source is available.
        is_bundled: True if source originated from pkgs/ tarball (bundled).
                    False if source was cloned from main_repo (git).

    The is_bundled flag tracks the source origin, not how it's accessed.
    Both bundled and cloned sources have a path where the source is available.

    Usage in executors:
        source = load_benchmark_source(benchmark_path, dest_dir, source_mode="main_repo")
        # source.path contains the source directory
        # source.is_bundled indicates whether it came from pkgs/ or git
    """

    path: Optional[Path]
    is_bundled: bool

    @property
    def requires_source_path(self) -> bool:
        """Check if CRS command needs --source-path argument."""
        return self.path is not None
