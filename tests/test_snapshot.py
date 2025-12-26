"""Unit tests for snapshot data structures and utilities."""

import json
import tarfile
import time

from crsbench.evaluation.snapshot import (
    SnapshotMetadata,
    SnapshotSummary,
    extract_snapshot,
    get_completion_marker_path,
    get_snapshot_archive_path,
    inspect_snapshot,
    is_snapshot_complete,
    list_snapshots,
    load_snapshot_metadata,
    validate_snapshot_structure,
)


class TestSnapshotMetadata:
    """Test SnapshotMetadata dataclass."""

    def test_create_metadata(self):
        """Test creating snapshot metadata."""
        metadata = SnapshotMetadata(
            cycle=1, timestamp=1234567890.0, elapsed_time=900.0, snapshot_period=900
        )

        assert metadata.cycle == 1
        assert metadata.timestamp == 1234567890.0
        assert metadata.elapsed_time == 900.0
        assert metadata.snapshot_period == 900

    def test_to_dict(self):
        """Test converting metadata to dict."""
        metadata = SnapshotMetadata(
            cycle=2, timestamp=1234567890.0, elapsed_time=1800.0, snapshot_period=600
        )

        data = metadata.to_dict()

        assert data["cycle"] == 2
        assert data["timestamp"] == 1234567890.0
        assert data["elapsed_time"] == 1800.0
        assert data["snapshot_period"] == 600

    def test_from_dict(self):
        """Test creating metadata from dict."""
        data = {
            "cycle": 3,
            "timestamp": 1234567890.0,
            "elapsed_time": 2700.0,
            "snapshot_period": 900,
        }

        metadata = SnapshotMetadata.from_dict(data)

        assert metadata.cycle == 3
        assert metadata.timestamp == 1234567890.0
        assert metadata.elapsed_time == 2700.0
        assert metadata.snapshot_period == 900

    def test_to_json(self):
        """Test JSON serialization."""
        metadata = SnapshotMetadata(
            cycle=1, timestamp=1234567890.0, elapsed_time=900.0, snapshot_period=900
        )

        json_str = metadata.to_json()
        data = json.loads(json_str)

        assert data["cycle"] == 1
        assert data["timestamp"] == 1234567890.0

    def test_from_json(self):
        """Test JSON deserialization."""
        json_str = '{"cycle": 4, "timestamp": 1234567890.0, "elapsed_time": 3600.0, "snapshot_period": 900}'

        metadata = SnapshotMetadata.from_json(json_str)

        assert metadata.cycle == 4
        assert metadata.elapsed_time == 3600.0


class TestSnapshotUtilities:
    """Test snapshot utility functions."""

    def test_is_snapshot_complete(self, tmp_path):
        """Test checking snapshot completion marker."""
        # No marker - not complete
        assert not is_snapshot_complete(tmp_path, 1)

        # Create marker
        marker = tmp_path / "snapshot-0001.complete"
        marker.touch()

        # Now complete
        assert is_snapshot_complete(tmp_path, 1)

    def test_get_snapshot_archive_path(self, tmp_path):
        """Test getting snapshot archive path."""
        path = get_snapshot_archive_path(tmp_path, 1)

        assert path == tmp_path / "snapshot-0001.tar.gz"
        assert path.name == "snapshot-0001.tar.gz"

    def test_get_completion_marker_path(self, tmp_path):
        """Test getting completion marker path."""
        path = get_completion_marker_path(tmp_path, 2)

        assert path == tmp_path / "snapshot-0002.complete"
        assert path.name == "snapshot-0002.complete"

    def test_list_snapshots_empty(self, tmp_path):
        """Test listing snapshots in empty directory."""
        snapshots = list_snapshots(tmp_path)

        assert snapshots == []

    def test_list_snapshots_with_snapshots(self, tmp_path):
        """Test listing multiple snapshots."""
        # Create snapshot archives
        (tmp_path / "snapshot-0001.tar.gz").touch()
        (tmp_path / "snapshot-0001.complete").touch()
        (tmp_path / "snapshot-0002.tar.gz").touch()
        (tmp_path / "snapshot-0002.complete").touch()
        (tmp_path / "snapshot-0003.tar.gz").touch()
        # No completion marker for 3

        snapshots = list_snapshots(tmp_path)

        assert len(snapshots) == 3
        assert snapshots[0].cycle == 1
        assert snapshots[0].is_complete is True
        assert snapshots[1].cycle == 2
        assert snapshots[1].is_complete is True
        assert snapshots[2].cycle == 3
        assert snapshots[2].is_complete is False

    def test_list_snapshots_sorted(self, tmp_path):
        """Test snapshots are sorted by cycle number."""
        # Create out of order
        (tmp_path / "snapshot-0003.tar.gz").touch()
        (tmp_path / "snapshot-0001.tar.gz").touch()
        (tmp_path / "snapshot-0002.tar.gz").touch()

        snapshots = list_snapshots(tmp_path)

        assert [s.cycle for s in snapshots] == [1, 2, 3]


class TestSnapshotArchive:
    """Test snapshot archive operations."""

    def create_test_archive(self, tmp_path, cycle=1):
        """Helper to create a test snapshot archive."""
        # Create temp directory with snapshot contents
        content_dir = tmp_path / f"snapshot-{cycle:04d}"
        content_dir.mkdir()

        # Create metadata
        metadata = SnapshotMetadata(
            cycle=cycle,
            timestamp=time.time(),
            elapsed_time=cycle * 900.0,
            snapshot_period=900,
        )
        (content_dir / "metadata.json").write_text(metadata.to_json())

        # Create some test files
        (content_dir / "config.yaml").write_text("experiment: test\n")
        (content_dir / "execution.json").write_text('{"trial_id": "test"}')

        # Create POVs
        pov_dir = content_dir / "povs"
        pov_dir.mkdir()
        (pov_dir / "pov_001").write_bytes(b"test pov data")

        # Create patches
        patches_dir = content_dir / "patches" / "pov_0"
        patches_dir.mkdir(parents=True)
        (patches_dir / "patch.diff").write_text("--- a/file.c\n+++ b/file.c\n")

        # Create tar.gz archive
        archive_path = tmp_path / f"snapshot-{cycle:04d}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in content_dir.rglob("*"):
                if item.is_file():
                    arcname = item.relative_to(content_dir)
                    tar.add(item, arcname=arcname)

        # Create completion marker
        marker_path = tmp_path / f"snapshot-{cycle:04d}.complete"
        marker_path.touch()

        # Cleanup temp directory
        import shutil

        shutil.rmtree(content_dir)

        return archive_path

    def test_load_snapshot_metadata(self, tmp_path):
        """Test loading metadata from archive."""
        archive_path = self.create_test_archive(tmp_path, cycle=1)

        metadata = load_snapshot_metadata(archive_path)

        assert metadata is not None
        assert metadata.cycle == 1
        assert metadata.elapsed_time == 900.0
        assert metadata.snapshot_period == 900

    def test_load_snapshot_metadata_missing_file(self, tmp_path):
        """Test loading metadata from non-existent archive."""
        archive_path = tmp_path / "nonexistent.tar.gz"

        metadata = load_snapshot_metadata(archive_path)

        assert metadata is None

    def test_inspect_snapshot(self, tmp_path):
        """Test inspecting snapshot archive."""
        archive_path = self.create_test_archive(tmp_path, cycle=2)

        summary = inspect_snapshot(archive_path)

        assert summary is not None
        assert summary.cycle == 2
        assert summary.is_complete is True
        assert summary.file_count > 0
        assert summary.archive_size_bytes > 0
        assert summary.metadata is not None
        assert summary.metadata.cycle == 2

    def test_inspect_snapshot_missing(self, tmp_path):
        """Test inspecting non-existent snapshot."""
        archive_path = tmp_path / "nonexistent.tar.gz"

        summary = inspect_snapshot(archive_path)

        assert summary is None

    def test_extract_snapshot(self, tmp_path):
        """Test extracting snapshot archive."""
        archive_path = self.create_test_archive(tmp_path, cycle=1)
        extract_dir = tmp_path / "extracted"

        success = extract_snapshot(archive_path, extract_dir)

        assert success is True
        assert extract_dir.exists()
        assert (extract_dir / "metadata.json").exists()
        assert (extract_dir / "config.yaml").exists()
        assert (extract_dir / "povs" / "pov_001").exists()
        assert (extract_dir / "patches" / "pov_0" / "patch.diff").exists()

    def test_extract_snapshot_missing(self, tmp_path):
        """Test extracting non-existent snapshot."""
        archive_path = tmp_path / "nonexistent.tar.gz"
        extract_dir = tmp_path / "extracted"

        success = extract_snapshot(archive_path, extract_dir)

        assert success is False

    def test_validate_snapshot_structure_valid(self, tmp_path):
        """Test validating a valid snapshot."""
        archive_path = self.create_test_archive(tmp_path, cycle=1)

        is_valid, errors = validate_snapshot_structure(archive_path)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_snapshot_structure_missing(self, tmp_path):
        """Test validating non-existent snapshot."""
        archive_path = tmp_path / "nonexistent.tar.gz"

        is_valid, errors = validate_snapshot_structure(archive_path)

        assert is_valid is False
        assert len(errors) > 0
        assert "does not exist" in errors[0]

    def test_validate_snapshot_structure_missing_metadata(self, tmp_path):
        """Test validating snapshot without metadata."""
        # Create archive without metadata
        content_dir = tmp_path / "snapshot-0001"
        content_dir.mkdir()
        (content_dir / "config.yaml").write_text("test")

        archive_path = tmp_path / "snapshot-0001.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(content_dir / "config.yaml", arcname="config.yaml")

        import shutil

        shutil.rmtree(content_dir)

        is_valid, errors = validate_snapshot_structure(archive_path)

        assert is_valid is False
        assert any("metadata.json" in error for error in errors)


class TestSnapshotSummary:
    """Test SnapshotSummary dataclass."""

    def test_create_summary(self, tmp_path):
        """Test creating snapshot summary."""
        archive_path = tmp_path / "snapshot-0001.tar.gz"
        archive_path.touch()

        summary = SnapshotSummary(
            cycle=1,
            archive_path=archive_path,
            is_complete=True,
            file_count=10,
            archive_size_bytes=1024,
        )

        assert summary.cycle == 1
        assert summary.archive_path == archive_path
        assert summary.is_complete is True
        assert summary.file_count == 10
        assert summary.archive_size_bytes == 1024
        assert summary.metadata is None

    def test_summary_with_metadata(self, tmp_path):
        """Test summary with metadata."""
        archive_path = tmp_path / "snapshot-0002.tar.gz"
        metadata = SnapshotMetadata(
            cycle=2, timestamp=time.time(), elapsed_time=1800.0, snapshot_period=900
        )

        summary = SnapshotSummary(
            cycle=2, archive_path=archive_path, is_complete=True, metadata=metadata
        )

        assert summary.metadata is not None
        assert summary.metadata.cycle == 2
        assert summary.metadata.elapsed_time == 1800.0
