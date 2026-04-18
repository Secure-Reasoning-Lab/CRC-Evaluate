"""Storage backends for dataset download/upload.

Each backend implements download and upload for a specific provider.
New backends can be added by implementing the download/upload functions
and registering them in BACKENDS.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Optional

import tenacity

from crsbench.dataset.registry import DatasetConfig
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _is_hf_rate_limit_or_server_error(exc: BaseException) -> bool:
    """Return True for HuggingFace errors worth retrying (429, 5xx)."""
    try:
        from huggingface_hub.errors import HfHubHTTPError
    except ImportError:
        from huggingface_hub.utils import HfHubHTTPError

    if not isinstance(exc, HfHubHTTPError):
        return False
    response = getattr(exc, "response", None)
    if response is None:
        return False
    return response.status_code == 429 or response.status_code >= 500


def _log_hf_retry(retry_state: tenacity.RetryCallState) -> None:
    """Log each retry attempt for HuggingFace downloads."""
    wait = retry_state.next_action.sleep if retry_state.next_action else 0  # type: ignore[union-attr]
    logger.warning(
        "HuggingFace download failed (attempt {}), retrying in {:.0f}s: {}",
        retry_state.attempt_number,
        wait,
        retry_state.outcome.exception() if retry_state.outcome else "unknown",
    )


def check_hf_token() -> tuple[bool, str]:
    """Check if HuggingFace token is configured and valid.

    Returns:
        (is_valid, message) tuple
    """
    from huggingface_hub import HfApi, get_token

    token = get_token()
    if not token:
        return False, ("No HuggingFace token found. Login with: hf auth login")

    try:
        api = HfApi()
        user_info = api.whoami()
        return True, f"Authenticated as: {user_info.get('name', 'unknown')}"
    except Exception as e:
        return False, (f"HuggingFace token invalid: {e}. Re-login with: hf auth login")


@tenacity.retry(
    retry=tenacity.retry_if_exception(_is_hf_rate_limit_or_server_error),
    wait=tenacity.wait_exponential(multiplier=1, min=30, max=300),
    stop=tenacity.stop_after_attempt(8),
    before_sleep=_log_hf_retry,
    reraise=True,
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
        repo_type=config.repo_type,
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
        repo_type=config.repo_type,
        allow_patterns=allow_patterns,
    )


def _delete_huggingface(
    config: DatasetConfig,
    paths: list[str],
    *,
    commit_message: Optional[str] = None,
) -> None:
    """Delete folders from a HuggingFace repo.

    Each path is treated as a folder inside the repo. One commit per path.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    for path_in_repo in paths:
        api.delete_folder(
            path_in_repo=path_in_repo,
            repo_id=config.location,
            repo_type=config.repo_type,
            commit_message=commit_message or f"Prune benchmark folder: {path_in_repo}",
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


def _delete_s3(
    config: DatasetConfig,
    paths: list[str],
    *,
    commit_message: Optional[str] = None,
) -> None:
    """Delete folders from S3."""
    raise NotImplementedError("S3 delete backend not yet implemented")


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


def _delete_azure(
    config: DatasetConfig,
    paths: list[str],
    *,
    commit_message: Optional[str] = None,
) -> None:
    """Delete folders from Azure Blob Storage."""
    raise NotImplementedError("Azure Blob Storage delete backend not yet implemented")


# Backend dispatch tables
type DownloadFn = Callable[..., Path]
type UploadFn = Callable[..., None]
type DeleteFn = Callable[..., None]

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

DELETE_BACKENDS: dict[str, DeleteFn] = {
    "huggingface": _delete_huggingface,
    "s3": _delete_s3,
    "azure": _delete_azure,
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


def delete(
    config: DatasetConfig,
    paths: list[str],
    *,
    commit_message: Optional[str] = None,
) -> None:
    """Delete folders from the configured backend.

    Args:
        config: Dataset configuration
        paths: Folder paths (repo-relative) to delete
        commit_message: Optional commit/audit message for backends that use one

    Raises:
        ValueError: If backend is not supported
    """
    if not paths:
        return
    backend_fn = DELETE_BACKENDS.get(config.backend)
    if backend_fn is None:
        supported = ", ".join(sorted(DELETE_BACKENDS.keys()))
        raise ValueError(
            f"Unsupported delete backend: {config.backend!r}. Supported: {supported}"
        )
    backend_fn(config, paths, commit_message=commit_message)
