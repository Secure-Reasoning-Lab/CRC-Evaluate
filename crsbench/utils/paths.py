"""Path utilities for CRSBench."""

from pathlib import Path

_crsbench_root: Path | None = None


def get_crsbench_root() -> Path:
    """Get the CRSBench repository root directory.

    Walks up from this file looking for pyproject.toml as a marker.
    Result is cached after the first call.

    Returns:
        Absolute path to the CRSBench repo root.

    Raises:
        RuntimeError: If pyproject.toml cannot be found.
    """
    global _crsbench_root  # noqa: PLW0603
    if _crsbench_root is not None:
        return _crsbench_root

    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (
            (current / "pyproject.toml").exists()
            and (current / "README.md").exists()
            and (current / "crsbench").is_dir()
        ):
            _crsbench_root = current
            return current
        current = current.parent

    msg = "Could not find CRSBench root (pyproject.toml + README.md + crsbench/ not found)"
    raise RuntimeError(msg)
