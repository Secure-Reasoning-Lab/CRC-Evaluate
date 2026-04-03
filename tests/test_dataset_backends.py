"""Unit tests for dataset backend retry/backoff logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.dataset.backends import (
    _download_huggingface,
    _is_hf_rate_limit_or_server_error,
)
from crsbench.dataset.registry import DatasetConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hf_config() -> DatasetConfig:
    return DatasetConfig(
        backend="huggingface",
        location="sslab-gatech/crsbench-dataset",
        prefixes=["afc-"],
        repo_type="dataset",
    )


def _make_hf_http_error(status_code: int) -> Exception:
    """Build a fake HfHubHTTPError with the given status code."""
    from huggingface_hub.errors import HfHubHTTPError

    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    exc = HfHubHTTPError(f"HTTP {status_code}", response=response)
    exc.response = response
    return exc


# ---------------------------------------------------------------------------
# _is_hf_rate_limit_or_server_error
# ---------------------------------------------------------------------------


class TestIsHfRateLimitOrServerError:
    """Tests for the retry predicate."""

    def test_returns_true_for_429(self) -> None:
        exc = _make_hf_http_error(429)
        assert _is_hf_rate_limit_or_server_error(exc) is True

    def test_returns_true_for_500(self) -> None:
        exc = _make_hf_http_error(500)
        assert _is_hf_rate_limit_or_server_error(exc) is True

    def test_returns_true_for_502(self) -> None:
        exc = _make_hf_http_error(502)
        assert _is_hf_rate_limit_or_server_error(exc) is True

    def test_returns_true_for_503(self) -> None:
        exc = _make_hf_http_error(503)
        assert _is_hf_rate_limit_or_server_error(exc) is True

    def test_returns_false_for_404(self) -> None:
        exc = _make_hf_http_error(404)
        assert _is_hf_rate_limit_or_server_error(exc) is False

    def test_returns_false_for_401(self) -> None:
        exc = _make_hf_http_error(401)
        assert _is_hf_rate_limit_or_server_error(exc) is False

    def test_returns_false_for_403(self) -> None:
        exc = _make_hf_http_error(403)
        assert _is_hf_rate_limit_or_server_error(exc) is False

    def test_returns_false_for_non_hf_exception(self) -> None:
        assert _is_hf_rate_limit_or_server_error(RuntimeError("boom")) is False

    def test_returns_false_for_hf_error_without_response(self) -> None:
        from huggingface_hub.errors import HfHubHTTPError

        exc = HfHubHTTPError("no response")
        exc.response = None
        assert _is_hf_rate_limit_or_server_error(exc) is False


# ---------------------------------------------------------------------------
# _download_huggingface retry behaviour
# ---------------------------------------------------------------------------


class TestDownloadHuggingfaceRetry:
    """Tests for retry/backoff on _download_huggingface."""

    @patch("huggingface_hub.snapshot_download")
    def test_succeeds_on_first_attempt(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        mock_download.return_value = str(tmp_path)
        config = _make_hf_config()

        result = _download_huggingface(config, tmp_path)

        assert result == tmp_path
        assert mock_download.call_count == 1

    @patch("huggingface_hub.snapshot_download")
    def test_retries_on_429_then_succeeds(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        mock_download.side_effect = [
            _make_hf_http_error(429),
            str(tmp_path),
        ]
        config = _make_hf_config()

        # Patch wait to avoid real sleeps in tests
        with patch.object(
            _download_huggingface.retry,
            "wait",
            return_value=0,  # type: ignore[attr-defined]
        ):
            result = _download_huggingface(config, tmp_path)

        assert result == tmp_path
        assert mock_download.call_count == 2

    @patch("huggingface_hub.snapshot_download")
    def test_retries_on_500_then_succeeds(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        mock_download.side_effect = [
            _make_hf_http_error(500),
            _make_hf_http_error(502),
            str(tmp_path),
        ]
        config = _make_hf_config()

        with patch.object(
            _download_huggingface.retry,
            "wait",
            return_value=0,  # type: ignore[attr-defined]
        ):
            result = _download_huggingface(config, tmp_path)

        assert result == tmp_path
        assert mock_download.call_count == 3

    @patch("huggingface_hub.snapshot_download")
    def test_does_not_retry_on_404(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        from huggingface_hub.errors import HfHubHTTPError

        mock_download.side_effect = _make_hf_http_error(404)
        config = _make_hf_config()

        with pytest.raises(HfHubHTTPError):
            _download_huggingface(config, tmp_path)

        assert mock_download.call_count == 1

    @patch("huggingface_hub.snapshot_download")
    def test_does_not_retry_on_401(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        from huggingface_hub.errors import HfHubHTTPError

        mock_download.side_effect = _make_hf_http_error(401)
        config = _make_hf_config()

        with pytest.raises(HfHubHTTPError):
            _download_huggingface(config, tmp_path)

        assert mock_download.call_count == 1

    @patch("huggingface_hub.snapshot_download")
    def test_exhausts_retries_and_reraises(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        from huggingface_hub.errors import HfHubHTTPError

        mock_download.side_effect = _make_hf_http_error(429)
        config = _make_hf_config()

        with (
            patch.object(
                _download_huggingface.retry,
                "wait",
                return_value=0,  # type: ignore[attr-defined]
            ),
            pytest.raises(HfHubHTTPError),
        ):
            _download_huggingface(config, tmp_path)

        # 8 attempts (stop_after_attempt(8))
        assert mock_download.call_count == 8
