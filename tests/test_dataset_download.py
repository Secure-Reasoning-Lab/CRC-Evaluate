"""Unit tests for dataset download completeness and retry behavior."""

from pathlib import Path
from unittest.mock import patch

import crsbench.dataset.download as dataset_download
import pytest
from crsbench.dataset.download import (
    IncompleteDatasetDownloadError,
    _incomplete_download_wait_seconds,
    _load_remote_manifest,
    _validate_downloaded_bundles,
    download_dataset,
)
from crsbench.dataset.manifest import BenchmarkManifestEntry
from crsbench.dataset.registry import DatasetConfig


def _hf_config() -> DatasetConfig:
    return DatasetConfig(
        backend="huggingface",
        location="sslab-gatech/crsbench-dataset",
        prefixes=["afc-"],
        repo_type="dataset",
    )


def _manifest_entry(name: str, **overrides: object) -> BenchmarkManifestEntry:
    defaults: dict[str, object] = {
        "benchmark": name,
        "benchmark_source_sha256": "bench-hash",
        "ground_truth_source_sha256": "",
        "has_ground_truth": False,
    }
    defaults.update(overrides)
    return BenchmarkManifestEntry(**defaults)  # type: ignore[arg-type]


def _write_bundle(staging_dir: Path, benchmark: str, archive_name: str) -> None:
    bundle_path = staging_dir / benchmark / archive_name
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text("bundle")


def test_download_dataset_retries_incomplete_staging_on_same_dir(
    tmp_path: Path,
) -> None:
    config = _hf_config()
    output_dir = tmp_path / "benchmarks"
    benchmark_a = "afc-alpha"
    benchmark_b = "afc-beta"
    remote_manifest = {
        benchmark_a: _manifest_entry(benchmark_a),
        benchmark_b: _manifest_entry(benchmark_b),
    }
    staging_dirs: list[Path] = []

    def fake_download(
        _config: DatasetConfig,
        staging_dir: Path,
        *,
        allow_patterns: list[str] | None = None,  # noqa: ARG001
    ) -> Path:
        staging_dirs.append(staging_dir)
        if len(staging_dirs) == 1:
            _write_bundle(staging_dir, benchmark_a, "benchmark.tar.gz")
        else:
            _write_bundle(staging_dir, benchmark_b, "benchmark.tar.gz")
        return staging_dir

    with (
        patch("crsbench.dataset.download.get_dataset", return_value=config),
        patch(
            "crsbench.dataset.download._load_remote_manifest",
            return_value=remote_manifest,
        ),
        patch(
            "crsbench.dataset.download.download", side_effect=fake_download
        ) as mock_download,
        patch(
            "crsbench.dataset.download.unbundle_all", return_value=2
        ) as mock_unbundle,
        patch.object(
            dataset_download._download_complete_staging.retry,
            "wait",
            return_value=0,
        ),
    ):
        result = download_dataset(
            "crsbench",
            output_dir,
            benchmarks=[benchmark_a, benchmark_b],
        )

    assert result == output_dir
    assert mock_download.call_count == 2
    assert len(staging_dirs) == 2
    assert staging_dirs[0] == staging_dirs[1]
    mock_unbundle.assert_called_once()


def test_validate_downloaded_bundles_requires_ground_truth_when_manifest_says_so(
    tmp_path: Path,
) -> None:
    benchmark = "afc-alpha"
    staging_dir = tmp_path / "staging"
    _write_bundle(staging_dir, benchmark, "benchmark.tar.gz")

    with pytest.raises(
        IncompleteDatasetDownloadError,
        match="missing ground-truth.tar.gz",
    ):
        _validate_downloaded_bundles(
            staging_dir=staging_dir,
            expected_benchmarks=[benchmark],
            remote_manifest={
                benchmark: _manifest_entry(
                    benchmark,
                    ground_truth_source_sha256="gt-hash",
                    has_ground_truth=True,
                )
            },
            no_ground_truth=False,
        )


def test_incomplete_download_wait_uses_five_minutes_plus_jitter() -> None:
    with patch("crsbench.dataset.download.random.uniform", return_value=23.0):
        assert _incomplete_download_wait_seconds() == 323.0


def test_download_dataset_continues_without_manifest_for_explicit_benchmarks(
    tmp_path: Path,
) -> None:
    config = _hf_config()
    output_dir = tmp_path / "benchmarks"
    benchmark = "afc-alpha"

    def fake_download(
        _config: DatasetConfig,
        staging_dir: Path,
        *,
        allow_patterns: list[str] | None = None,  # noqa: ARG001
    ) -> Path:
        _write_bundle(staging_dir, benchmark, "benchmark.tar.gz")
        return staging_dir

    with (
        patch("crsbench.dataset.download.get_dataset", return_value=config),
        patch(
            "crsbench.dataset.download._load_remote_manifest",
            side_effect=RuntimeError("manifest unavailable"),
        ),
        patch("crsbench.dataset.download.download", side_effect=fake_download),
        patch(
            "crsbench.dataset.download.unbundle_all", return_value=1
        ) as mock_unbundle,
    ):
        result = download_dataset("crsbench", output_dir, benchmarks=[benchmark])

    assert result == output_dir
    mock_unbundle.assert_called_once()


def test_load_remote_manifest_propagates_backend_errors() -> None:
    config = _hf_config()

    with patch(
        "crsbench.dataset.download.download",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            _load_remote_manifest(config)
