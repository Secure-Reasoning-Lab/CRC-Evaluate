"""Storage backends for dataset download/upload.

Each backend implements download and upload for a specific provider.
New backends can be added by implementing the download/upload functions
and registering them in BACKENDS.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from crsbench.dataset.registry import DatasetConfig
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def check_hf_token() -> tuple[bool, str]:
    """Check if HuggingFace token is configured and valid.

    Returns:
        (is_valid, message) tuple
    """
    from huggingface_hub import HfApi

    api = HfApi()
    token = api.token
    if not token:
        return False, ("No HuggingFace token found. Login with: huggingface-cli login")

    try:
        user_info = api.whoami()
        return True, f"Authenticated as: {user_info.get('name', 'unknown')}"
    except Exception as e:
        return False, (
            f"HuggingFace token invalid: {e}. Re-login with: huggingface-cli login"
        )


def _download_huggingface(
    config: DatasetConfig,
    output_dir: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> Path:
    """Download from HuggingFace using snapshot_download."""
    from huggingface_hub import snapshot_download

    result = snapshot_download(
        repo_id=config.location,
        repo_type=config.repo_type or "dataset",
        local_dir=str(output_dir),
        allow_patterns=allow_patterns,
    )
    return Path(result)


def _upload_huggingface(
    config: DatasetConfig,
    folder_path: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> None:
    """Upload to HuggingFace using upload_large_folder for reliability."""
    from huggingface_hub import HfApi

    api = HfApi()
    api.upload_large_folder(
        folder_path=str(folder_path),
        repo_id=config.location,
        repo_type=config.repo_type or "dataset",
        allow_patterns=allow_patterns,
    )


# -- S3 backend (placeholder) --


def _download_s3(
    config: DatasetConfig,
    output_dir: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> Path:
    """Download from S3."""
    raise NotImplementedError("S3 download backend not yet implemented")


def _upload_s3(
    config: DatasetConfig,
    folder_path: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> None:
    """Upload to S3."""
    raise NotImplementedError("S3 upload backend not yet implemented")


# -- Azure Blob Storage backend (placeholder) --


def _download_azure(
    config: DatasetConfig,
    output_dir: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> Path:
    """Download from Azure Blob Storage."""
    raise NotImplementedError("Azure Blob Storage download backend not yet implemented")


def _upload_azure(
    config: DatasetConfig,
    folder_path: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> None:
    """Upload to Azure Blob Storage."""
    raise NotImplementedError("Azure Blob Storage upload backend not yet implemented")


# Backend dispatch tables
type DownloadFn = Callable[..., Path]
type UploadFn = Callable[..., None]

DOWNLOAD_BACKENDS: dict[str, DownloadFn] = {
    "huggingface": _download_huggingface,
    "s3": _download_s3,
    "azure": _download_azure,
}

UPLOAD_BACKENDS: dict[str, UploadFn] = {
    "huggingface": _upload_huggingface,
    "s3": _upload_s3,
    "azure": _upload_azure,
}


def download(
    config: DatasetConfig,
    output_dir: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> Path:
    """Download using the appropriate backend.

    Args:
        config: Dataset configuration
        output_dir: Directory to download into
        allow_patterns: Optional patterns to filter files

    Returns:
        Path to the downloaded directory

    Raises:
        ValueError: If backend is not supported
    """
    backend_fn = DOWNLOAD_BACKENDS.get(config.backend)
    if backend_fn is None:
        supported = ", ".join(sorted(DOWNLOAD_BACKENDS.keys()))
        raise ValueError(
            f"Unsupported download backend: {config.backend!r}. Supported: {supported}"
        )
    return backend_fn(config, output_dir, allow_patterns=allow_patterns)


def upload(
    config: DatasetConfig,
    folder_path: Path,
    *,
    allow_patterns: Optional[list[str]] = None,
) -> None:
    """Upload using the appropriate backend.

    Args:
        config: Dataset configuration
        folder_path: Directory to upload from
        allow_patterns: Optional patterns to filter files

    Raises:
        ValueError: If backend is not supported
    """
    backend_fn = UPLOAD_BACKENDS.get(config.backend)
    if backend_fn is None:
        supported = ", ".join(sorted(UPLOAD_BACKENDS.keys()))
        raise ValueError(
            f"Unsupported upload backend: {config.backend!r}. Supported: {supported}"
        )
    backend_fn(config, folder_path, allow_patterns=allow_patterns)
