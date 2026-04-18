"""Unit tests for upload_dataset prune semantics and backend delete dispatch."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.dataset.backends import _delete_huggingface, delete
from crsbench.dataset.manifest import BenchmarkManifestEntry
from crsbench.dataset.registry import DatasetConfig
from crsbench.dataset.upload import _compose_manifest, upload_dataset


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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
        "ground_truth_source_sha256": "gt-hash",
        "benchmark_archive_sha256": "archive-hash",
        "benchmark_archive_size": 1,
        "has_ground_truth": False,
    }
    defaults.update(overrides)
    return BenchmarkManifestEntry(**defaults)  # type: ignore[arg-type]


class TestComposeManifest:
    """_compose_manifest merges remote + updates and drops pruned entries."""

    def test_merges_updates_over_remote(self) -> None:
        remote = {"afc-a": _manifest_entry("afc-a")}
        updates = {"afc-a": _manifest_entry("afc-a", benchmark_source_sha256="new")}

        merged = _compose_manifest(remote, updates, set())

        assert merged["afc-a"].benchmark_source_sha256 == "new"

    def test_preserves_remote_only_entries_when_no_stale(self) -> None:
        remote = {"afc-a": _manifest_entry("afc-a"), "afc-b": _manifest_entry("afc-b")}
        updates: dict[str, BenchmarkManifestEntry] = {}

        merged = _compose_manifest(remote, updates, set())

        assert set(merged.keys()) == {"afc-a", "afc-b"}

    def test_drops_stale_entries(self) -> None:
        remote = {"afc-a": _manifest_entry("afc-a"), "afc-b": _manifest_entry("afc-b")}
        updates = {"afc-a": _manifest_entry("afc-a")}

        merged = _compose_manifest(remote, updates, {"afc-b"})

        assert set(merged.keys()) == {"afc-a"}

    def test_stale_overrides_updates(self) -> None:
        # If a name appears in both updates and stale, stale wins (drops it).
        remote = {"afc-a": _manifest_entry("afc-a")}
        updates = {"afc-a": _manifest_entry("afc-a")}

        merged = _compose_manifest(remote, updates, {"afc-a"})

        assert merged == {}


class TestUploadDatasetPruneGuardrails:
    """prune+benchmarks combination is rejected before any remote calls."""

    def test_prune_with_benchmarks_raises(self, tmp_path: Path) -> None:
        benchmarks_dir = tmp_path / "benchmarks"
        _write(benchmarks_dir / "afc-a" / "Dockerfile", "FROM scratch\n")

        with pytest.raises(ValueError, match="prune=True is incompatible"):
            upload_dataset(
                "crsbench",
                benchmarks_dir,
                benchmarks=["afc-a"],
                prune=True,
            )


class _FakeUpload:
    """Captures folder upload() invocations + staging file contents at call time.

    The real upload_dataset wraps staging in a TemporaryDirectory that is
    deleted once the with-block exits, so we must snapshot file contents
    while the context is still live.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Path, list[str] | None]] = []
        self.staging_snapshots: list[dict[str, str]] = []

    def __call__(
        self,
        config: DatasetConfig,  # noqa: ARG002
        folder_path: Path,
        *,
        allow_patterns: list[str] | None = None,
    ) -> None:
        folder = Path(folder_path)
        self.calls.append((folder, allow_patterns))
        snapshot: dict[str, str] = {}
        for path in folder.rglob("*"):
            if path.is_file():
                snapshot[str(path.relative_to(folder))] = path.read_text()
        self.staging_snapshots.append(snapshot)


class TestUploadDatasetPruneDryRun:
    """Dry-run must log stale entries without calling delete/upload backends."""

    def test_dry_run_skips_all_remote_side_effects(self, tmp_path: Path) -> None:
        benchmarks_dir = tmp_path / "benchmarks"
        _write(benchmarks_dir / "afc-kept" / "Dockerfile", "FROM scratch\n")

        remote_manifest = {
            "afc-kept": _manifest_entry("afc-kept"),
            "afc-stale": _manifest_entry("afc-stale"),
        }

        with (
            patch(
                "crsbench.dataset.upload._load_remote_manifest",
                return_value=remote_manifest,
            ),
            patch(
                "crsbench.dataset.upload._load_remote_files",
                return_value={"afc-kept/benchmark.tar.gz"},
            ),
            patch("crsbench.dataset.upload.delete") as mock_delete,
            patch("crsbench.dataset.upload.upload") as mock_upload,
            patch("crsbench.dataset.upload._upload_card_files") as mock_cards,
            patch(
                "crsbench.dataset.upload.get_dataset",
                return_value=_hf_config(),
            ),
        ):
            upload_dataset(
                "crsbench",
                benchmarks_dir,
                dry_run=True,
                prune=True,
            )

            mock_delete.assert_not_called()
            mock_upload.assert_not_called()
            mock_cards.assert_called_once()
            # Card upload delegates dry-run handling to the helper itself.
            assert mock_cards.call_args.kwargs == {"dry_run": True}


class TestUploadDatasetPruneExecution:
    """prune path deletes stale remote folders and drops them from manifest."""

    def test_prune_deletes_stale_and_reuploads_manifest(self, tmp_path: Path) -> None:
        benchmarks_dir = tmp_path / "benchmarks"
        _write(benchmarks_dir / "afc-kept" / "Dockerfile", "FROM scratch\n")

        kept_entry = _manifest_entry("afc-kept")
        # Pre-fill hashes so afc-kept is considered unchanged and the upload
        # path takes the manifest-only branch.
        from crsbench.dataset.manifest import compute_source_fingerprints

        bench_hash, gt_hash = compute_source_fingerprints(benchmarks_dir / "afc-kept")
        kept_entry.benchmark_source_sha256 = bench_hash
        kept_entry.ground_truth_source_sha256 = gt_hash

        remote_manifest = {
            "afc-kept": kept_entry,
            "afc-stale": _manifest_entry("afc-stale"),
        }

        fake_upload = _FakeUpload()

        with (
            patch(
                "crsbench.dataset.upload._load_remote_manifest",
                return_value=remote_manifest,
            ),
            patch(
                "crsbench.dataset.upload._load_remote_files",
                return_value={
                    "afc-kept/benchmark.tar.gz",
                    "afc-stale/benchmark.tar.gz",
                },
            ),
            patch("crsbench.dataset.upload.delete") as mock_delete,
            patch("crsbench.dataset.upload.upload", side_effect=fake_upload),
            patch("crsbench.dataset.upload._upload_card_files") as mock_cards,
            patch(
                "crsbench.dataset.upload.get_dataset",
                return_value=_hf_config(),
            ),
        ):
            upload_dataset("crsbench", benchmarks_dir, prune=True)

            mock_delete.assert_called_once()
            args, kwargs = mock_delete.call_args
            # delete() receives sorted stale names.
            assert args[1] == ["afc-stale"]
            assert kwargs == {}

            mock_cards.assert_called_once_with(_hf_config())

        # Manifest-only branch passes allow_patterns restricting to index files.
        assert len(fake_upload.calls) == 1
        _, allow_patterns = fake_upload.calls[0]
        assert allow_patterns is not None
        assert any("benchmarks.jsonl" in p for p in allow_patterns)

        # And the uploaded index files must have afc-stale removed.
        snapshot = fake_upload.staging_snapshots[0]
        uploaded = snapshot["index/benchmarks.jsonl"]
        assert "afc-kept" in uploaded
        assert "afc-stale" not in uploaded


class TestDeleteDispatch:
    """delete() routes to the configured backend."""

    def test_noop_for_empty_paths(self) -> None:
        # Empty paths must short-circuit before any backend lookup.
        fake_backend = MagicMock()
        with patch.dict(
            "crsbench.dataset.backends.DELETE_BACKENDS",
            {"huggingface": fake_backend},
            clear=False,
        ):
            delete(_hf_config(), [])
            fake_backend.assert_not_called()

    def test_routes_to_huggingface(self) -> None:
        fake_backend = MagicMock()
        with patch.dict(
            "crsbench.dataset.backends.DELETE_BACKENDS",
            {"huggingface": fake_backend},
            clear=False,
        ):
            delete(_hf_config(), ["afc-stale"])
            fake_backend.assert_called_once()
            args, kwargs = fake_backend.call_args
            assert args[1] == ["afc-stale"]
            assert kwargs == {"commit_message": None}

    def test_rejects_unknown_backend(self) -> None:
        config = DatasetConfig(backend="unknown", location="x", prefixes=["afc-"])
        with pytest.raises(ValueError, match="Unsupported delete backend"):
            delete(config, ["afc-stale"])


class TestDeleteHuggingFace:
    """_delete_huggingface calls delete_folder once per path."""

    def test_calls_delete_folder_per_path(self) -> None:
        with patch("huggingface_hub.HfApi") as mock_api_cls:
            api = mock_api_cls.return_value
            _delete_huggingface(
                _hf_config(),
                ["afc-a", "afc-b"],
                commit_message="prune",
            )

            assert api.delete_folder.call_count == 2
            first, second = api.delete_folder.call_args_list
            assert first.kwargs["path_in_repo"] == "afc-a"
            assert second.kwargs["path_in_repo"] == "afc-b"
            assert first.kwargs["commit_message"] == "prune"
            assert first.kwargs["repo_id"] == "sslab-gatech/crsbench-dataset"
            assert first.kwargs["repo_type"] == "dataset"

    def test_default_commit_message_names_the_path(self) -> None:
        with patch("huggingface_hub.HfApi") as mock_api_cls:
            api = mock_api_cls.return_value
            _delete_huggingface(_hf_config(), ["afc-a"])

            assert (
                api.delete_folder.call_args.kwargs["commit_message"]
                == "Prune benchmark folder: afc-a"
            )
