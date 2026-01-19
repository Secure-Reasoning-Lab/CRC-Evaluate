"""Data models for benchmark runtime loading."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BenchmarkSource:
    """Represents loaded benchmark source for CRS execution.

    Attributes:
        path: Path to source directory. None if using Docker's built-in
              source (from pkgs/ tarball).
        is_bundled: True if source is from pkgs/ (bundled in Docker image).
                    When True, path is None and CRS should use Docker's source.

    Usage in executors:
        source = load_benchmark_source(benchmark_path, dest_dir)
        if source.path:
            # Pass source path to CRS command
            cmd.extend(["--source-path", str(source.path)])
        # If source.is_bundled, don't pass --source-path (Docker has it)
    """

    path: Optional[Path]
    is_bundled: bool

    def __post_init__(self) -> None:
        """Validate state consistency."""
        if self.is_bundled and self.path is not None:
            raise ValueError("Bundled source should have path=None")

    @property
    def requires_source_path(self) -> bool:
        """Check if CRS command needs --source-path argument."""
        return not self.is_bundled and self.path is not None
