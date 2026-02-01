"""Unit tests for POVStore.

Tests for crsbench/evaluation/verification/pov/store.py.
"""

import json
from pathlib import Path

import pytest
from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.store import POVStore
from crsbench.evaluation.verification.utils import compute_content_hash


class TestPOVStoreInit:
    """Tests for POVStore initialization."""

    def test_creates_directories(self, tmp_path: Path) -> None:
        """Test that store creates required directory structure."""
        store_dir = tmp_path / "povs"
        POVStore(store_dir)

        assert store_dir.exists()
        assert (store_dir / "cpvs").exists()
        assert (store_dir / "unintended" / "blobs").exists()
        assert (store_dir / "unintended" / "crash_logs").exists()
        assert (store_dir / "snapshots").exists()

    def test_initial_state_empty(self, tmp_path: Path) -> None:
        """Test that store initializes with empty state."""
        store = POVStore(tmp_path / "povs")

        assert len(store.povs) == 0
        assert len(store.cpv_to_first_pov) == 0


class TestPOVStoreAddPov:
    """Tests for POVStore.add_pov method."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> POVStore:
        """Create a store instance."""
        return POVStore(tmp_path / "povs")

    @pytest.fixture
    def pov_file(self, tmp_path: Path) -> Path:
        """Create a test POV file."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test pov content")
        return pov

    def test_add_new_pov(self, store: POVStore, pov_file: Path) -> None:
        """Test adding a new POV."""
        pov_hash, is_new = store.add_pov(
            pov_file,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            verification_duration=45.0,
        )

        assert is_new is True
        assert len(pov_hash) == 16
        assert pov_hash in store.povs
        assert store.povs[pov_hash].status == PovVerificationStatus.CPV
        assert store.povs[pov_hash].cpv_matched == ["cpv_0"]
        assert store.povs[pov_hash].verification_duration == 45.0

    def test_add_duplicate_pov_returns_false(
        self, store: POVStore, pov_file: Path
    ) -> None:
        """Test adding duplicate POV returns is_new=False."""
        hash1, is_new1 = store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"])
        hash2, is_new2 = store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"])

        assert hash1 == hash2
        assert is_new1 is True
        assert is_new2 is False

    def test_add_pov_tracks_cpv_first_pov(
        self, store: POVStore, pov_file: Path
    ) -> None:
        """Test that first POV for each CPV is tracked with discovery info."""
        pov_hash, _ = store.add_pov(
            pov_file, PovVerificationStatus.CPV, ["cpv_0", "cpv_1"]
        )

        # Now cpv_to_first_pov stores {pov_hash, discovery_ts, relative_time}
        assert store.cpv_to_first_pov["cpv_0"]["pov_hash"] == pov_hash
        assert store.cpv_to_first_pov["cpv_1"]["pov_hash"] == pov_hash
        assert "discovery_ts" in store.cpv_to_first_pov["cpv_0"]
        assert "relative_time" in store.cpv_to_first_pov["cpv_0"]

    def test_add_pov_keeps_first_cpv_mapping(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test that first POV mapping isn't overwritten by later POVs."""
        pov1 = tmp_path / "pov1.bin"
        pov2 = tmp_path / "pov2.bin"
        pov1.write_bytes(b"content_a")
        pov2.write_bytes(b"content_b")

        hash1, _ = store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        hash2, _ = store.add_pov(pov2, PovVerificationStatus.CPV, ["cpv_0"])

        # First POV should still be mapped
        assert store.cpv_to_first_pov["cpv_0"]["pov_hash"] == hash1
        assert hash1 != hash2

    def test_add_pov_with_custom_timestamp(
        self, store: POVStore, pov_file: Path
    ) -> None:
        """Test adding POV with custom timestamp."""
        custom_ts = 1704672000.0
        store.add_pov(
            pov_file,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            timestamp=custom_ts,
        )

        pov_hash = compute_content_hash(pov_file)
        assert store.povs[pov_hash].first_seen_ts == custom_ts

    def test_add_pov_keeps_earliest_timestamp(
        self, store: POVStore, pov_file: Path
    ) -> None:
        """Test that duplicate POV keeps earliest timestamp."""
        store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"], timestamp=1000.0)
        store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"], timestamp=500.0)

        pov_hash = compute_content_hash(pov_file)
        assert store.povs[pov_hash].first_seen_ts == 500.0


class TestPOVStoreUniquePov:
    """Tests for POVStore.store_unique_pov method."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> POVStore:
        """Create a store instance."""
        return POVStore(tmp_path / "povs")

    def test_store_unique_pov_cpv(self, store: POVStore, tmp_path: Path) -> None:
        """Test storing unique POV file for CPV status."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash = compute_content_hash(pov)

        stored_path = store.store_unique_pov(
            pov, pov_hash, PovVerificationStatus.CPV, ["cpv_0"]
        )

        assert stored_path.exists()
        assert stored_path.name == f"{pov_hash}.blob"
        assert stored_path.read_bytes() == b"test content"
        # Should be under cpvs/cpv_0/blobs/
        assert "cpvs" in str(stored_path)
        assert "cpv_0" in str(stored_path)

    def test_store_unique_pov_unintended_crash(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test storing unique POV file for UNINTENDED_CRASH status."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"unintended crash content")
        pov_hash = compute_content_hash(pov)

        stored_path = store.store_unique_pov(
            pov, pov_hash, PovVerificationStatus.UNINTENDED_CRASH, []
        )

        assert stored_path.exists()
        assert "unintended" in str(stored_path)

    def test_store_unique_pov_idempotent(self, store: POVStore, tmp_path: Path) -> None:
        """Test storing same POV twice doesn't duplicate."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash = compute_content_hash(pov)

        path1 = store.store_unique_pov(
            pov, pov_hash, PovVerificationStatus.CPV, ["cpv_0"]
        )
        path2 = store.store_unique_pov(
            pov, pov_hash, PovVerificationStatus.CPV, ["cpv_0"]
        )

        assert path1 == path2


class TestPOVStoreCrashLog:
    """Tests for POVStore.store_crash_log method."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> POVStore:
        """Create a store instance."""
        return POVStore(tmp_path / "povs")

    def test_store_crash_log_cpv(self, store: POVStore, tmp_path: Path) -> None:
        """Test storing crash log for CPV status."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        crash_log = "ASAN: heap-buffer-overflow\n"
        log_path = store.store_crash_log(
            pov_hash, crash_log, PovVerificationStatus.CPV, ["cpv_0"]
        )

        assert log_path.exists()
        assert log_path.name == f"{pov_hash}.log"
        assert log_path.read_text() == crash_log
        # Should be under cpvs/cpv_0/crash_logs/
        assert "cpvs" in str(log_path)
        assert "cpv_0" in str(log_path)

    def test_store_crash_log_unintended_crash(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test storing crash log for UNINTENDED_CRASH status."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"unintended crash content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.UNINTENDED_CRASH, [])

        crash_log = "ASAN: use-after-free\n"
        log_path = store.store_crash_log(
            pov_hash, crash_log, PovVerificationStatus.UNINTENDED_CRASH, []
        )

        assert log_path.exists()
        assert "unintended" in str(log_path)

    def test_store_crash_log_updates_entry(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test that storing crash log updates POV entry."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        store.store_crash_log(
            pov_hash, "crash log content", PovVerificationStatus.CPV, ["cpv_0"]
        )

        entry = store.get_pov(pov_hash)
        assert entry is not None
        assert entry.crash_log_path == f"cpvs/cpv_0/crash_logs/{pov_hash}.log"

    def test_store_crash_log_with_variant_name(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test storing crash log with variant name suffix."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        crash_log = "ASAN: heap-buffer-overflow\n"
        log_path = store.store_crash_log(
            pov_hash,
            crash_log,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            variant_name="patched",
        )

        assert log_path.exists()
        assert log_path.name == f"{pov_hash}-patched.log"
        assert log_path.read_text() == crash_log

    def test_store_crash_log_with_variant_does_not_update_entry(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """Test that per-variant crash log does not update main POV entry."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        # Store main crash log first
        store.store_crash_log(
            pov_hash, "main crash log", PovVerificationStatus.CPV, ["cpv_0"]
        )
        entry = store.get_pov(pov_hash)
        assert entry is not None
        main_log_path = entry.crash_log_path

        # Store variant crash log
        store.store_crash_log(
            pov_hash,
            "variant crash log",
            PovVerificationStatus.CPV,
            ["cpv_0"],
            variant_name="vuln",
        )

        # Main entry should not be changed
        entry = store.get_pov(pov_hash)
        assert entry is not None
        assert entry.crash_log_path == main_log_path


class TestPOVStoreQueries:
    """Tests for POVStore query methods."""

    @pytest.fixture
    def store_with_data(self, tmp_path: Path) -> POVStore:
        """Create a store with test data."""
        store = POVStore(tmp_path / "povs")

        # Add POVs
        pov1 = tmp_path / "pov1.bin"
        pov2 = tmp_path / "pov2.bin"
        pov3 = tmp_path / "pov3.bin"
        pov1.write_bytes(b"content_a")
        pov2.write_bytes(b"content_b")
        pov3.write_bytes(b"content_c")

        store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        store.add_pov(pov2, PovVerificationStatus.UNINTENDED_CRASH, [])
        store.add_pov(pov3, PovVerificationStatus.NOT_VULNERABLE, [])

        return store

    def test_get_pov(self, store_with_data: POVStore, tmp_path: Path) -> None:
        """Test getting POV entry by hash."""
        pov1 = tmp_path / "pov1.bin"
        pov_hash = compute_content_hash(pov1)

        entry = store_with_data.get_pov(pov_hash)

        assert entry is not None
        assert entry.status == PovVerificationStatus.CPV
        assert entry.cpv_matched == ["cpv_0"]

    def test_get_pov_not_found(self, store_with_data: POVStore) -> None:
        """Test getting non-existent POV returns None."""
        entry = store_with_data.get_pov("nonexistent")
        assert entry is None

    def test_get_cpvs_found(self, store_with_data: POVStore, tmp_path: Path) -> None:
        """Test getting discovered CPVs."""
        cpvs = store_with_data.get_cpvs_found()
        assert cpvs == {"cpv_0"}

    def test_get_tested_hashes(self, store_with_data: POVStore, tmp_path: Path) -> None:
        """Test getting tested POV hashes."""
        hashes = store_with_data.get_tested_hashes()

        pov1_hash = compute_content_hash(tmp_path / "pov1.bin")
        pov2_hash = compute_content_hash(tmp_path / "pov2.bin")
        pov3_hash = compute_content_hash(tmp_path / "pov3.bin")

        assert pov1_hash in hashes
        assert pov2_hash in hashes
        assert pov3_hash in hashes
        assert len(hashes) == 3

    def test_is_already_tested(self, store_with_data: POVStore, tmp_path: Path) -> None:
        """Test checking if POV is already tested."""
        pov1 = tmp_path / "pov1.bin"
        new_pov = tmp_path / "new.bin"
        new_pov.write_bytes(b"new content")

        assert store_with_data.is_already_tested(pov1) is True
        assert store_with_data.is_already_tested(new_pov) is False

    def test_get_stats(self, store_with_data: POVStore) -> None:
        """Test getting store statistics."""
        stats = store_with_data.get_stats()

        assert stats["total_povs"] == 3
        assert stats["cpvs_found"] == 1
        assert stats["unintended_crashes"] == 1
        assert stats["errors"] == 0


class TestPOVStorePersistence:
    """Tests for POVStore save/load methods."""

    @pytest.fixture
    def store_with_data(self, tmp_path: Path) -> POVStore:
        """Create a store with test data."""
        store = POVStore(tmp_path / "povs")

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0", "cpv_1"])

        return store

    def test_save_and_load(self, store_with_data: POVStore, tmp_path: Path) -> None:
        """Test saving and loading store state."""
        # Save
        store_with_data.save()

        # Create new store and load
        new_store = POVStore(tmp_path / "povs")
        new_store.load()

        # Verify data
        assert len(new_store.povs) == len(store_with_data.povs)
        assert new_store.cpv_to_first_pov == store_with_data.cpv_to_first_pov

        pov_hash = compute_content_hash(tmp_path / "test.pov")
        assert new_store.povs[pov_hash].status == PovVerificationStatus.CPV
        assert new_store.povs[pov_hash].cpv_matched == ["cpv_0", "cpv_1"]

    def test_save_to_custom_path(
        self, store_with_data: POVStore, tmp_path: Path
    ) -> None:
        """Test saving to custom path."""
        custom_path = tmp_path / "custom" / "store.json"
        store_with_data.save(custom_path)

        assert custom_path.exists()

        # Verify JSON structure
        data = json.loads(custom_path.read_text())
        assert "povs" in data
        assert "cpv_to_first_pov" in data

    def test_load_from_custom_path(
        self, store_with_data: POVStore, tmp_path: Path
    ) -> None:
        """Test loading from custom path."""
        custom_path = tmp_path / "custom" / "store.json"
        store_with_data.save(custom_path)

        new_store = POVStore(tmp_path / "new_store")
        new_store.load(custom_path)

        assert len(new_store.povs) == len(store_with_data.povs)

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        """Test loading non-existent file raises FileNotFoundError."""
        store = POVStore(tmp_path / "povs")

        with pytest.raises(FileNotFoundError):
            store.load(tmp_path / "nonexistent.json")


class TestPOVStoreTimestamps:
    """Tests for POVStore timestamp features."""

    def test_crs_run_start_time_default(self, tmp_path: Path) -> None:
        """Test that crs_run_start_time defaults to current time."""
        import time

        before = time.time()
        store = POVStore(tmp_path / "povs")
        after = time.time()

        assert before <= store.crs_run_start_time <= after

    def test_crs_run_start_time_explicit(self, tmp_path: Path) -> None:
        """Test setting explicit crs_run_start_time."""
        custom_time = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=custom_time)

        assert store.crs_run_start_time == custom_time

    def test_crs_run_start_time_persisted(self, tmp_path: Path) -> None:
        """Test that crs_run_start_time is saved and loaded."""
        custom_time = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=custom_time)

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])
        store.save()

        # Load into new store
        new_store = POVStore(tmp_path / "povs")
        new_store.load()

        assert new_store.crs_run_start_time == custom_time

    def test_file_mtime_recorded(self, tmp_path: Path) -> None:
        """Test that file_mtime is recorded when adding POV."""
        import os

        store = POVStore(tmp_path / "povs")

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")

        # Set a specific mtime
        mtime = 1700000000.0
        os.utime(pov, (mtime, mtime))

        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        entry = store.get_pov(pov_hash)
        assert entry is not None
        assert entry.file_mtime == mtime

    def test_file_mtime_persisted(self, tmp_path: Path) -> None:
        """Test that file_mtime is saved and loaded."""
        import os

        store = POVStore(tmp_path / "povs")

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")

        mtime = 1700000000.0
        os.utime(pov, (mtime, mtime))

        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])
        store.save()

        # Load into new store
        new_store = POVStore(tmp_path / "povs")
        new_store.load()

        entry = new_store.get_pov(pov_hash)
        assert entry is not None
        assert entry.file_mtime == mtime

    def test_relative_time_calculation(self, tmp_path: Path) -> None:
        """Test that relative time can be calculated from file_mtime and crs_run_start_time."""
        import os

        crs_start = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=crs_start)

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")

        # POV created 10 seconds after CRS start
        mtime = crs_start + 10.0
        os.utime(pov, (mtime, mtime))

        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        entry = store.get_pov(pov_hash)
        assert entry is not None
        assert entry.file_mtime is not None

        relative_time = entry.file_mtime - store.crs_run_start_time
        assert relative_time == 10.0

    def test_cpv_discovery_timestamps(self, tmp_path: Path) -> None:
        """Test that per-CPV discovery timestamps are recorded."""
        crs_start = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=crs_start)

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")

        # Add POV with specific timestamp (simulating discovery time)
        discovery_ts = crs_start + 30.0
        pov_hash, _ = store.add_pov(
            pov,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            timestamp=discovery_ts,
        )

        # Verify per-CPV discovery info
        cpv_info = store.cpv_to_first_pov["cpv_0"]
        assert cpv_info["pov_hash"] == pov_hash
        assert cpv_info["discovery_ts"] == discovery_ts
        assert cpv_info["relative_time"] == 30.0  # discovery_ts - crs_start

    def test_cpv_discovery_timestamps_persisted(self, tmp_path: Path) -> None:
        """Test that per-CPV discovery timestamps are saved and loaded."""
        crs_start = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=crs_start)

        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")

        discovery_ts = crs_start + 45.0
        pov_hash, _ = store.add_pov(
            pov,
            PovVerificationStatus.CPV,
            ["cpv_0", "cpv_1"],
            timestamp=discovery_ts,
        )
        store.save()

        # Load into new store
        new_store = POVStore(tmp_path / "povs")
        new_store.load()

        # Verify all CPV discovery info is preserved
        for cpv_id in ["cpv_0", "cpv_1"]:
            cpv_info = new_store.cpv_to_first_pov[cpv_id]
            assert cpv_info["pov_hash"] == pov_hash
            assert cpv_info["discovery_ts"] == discovery_ts
            assert cpv_info["relative_time"] == 45.0

    def test_multiple_cpv_discovery_order(self, tmp_path: Path) -> None:
        """Test that discovery timestamps track each CPV's first discovery."""
        crs_start = 1700000000.0
        store = POVStore(tmp_path / "povs", crs_run_start_time=crs_start)

        pov1 = tmp_path / "pov1.bin"
        pov2 = tmp_path / "pov2.bin"
        pov1.write_bytes(b"content_a")
        pov2.write_bytes(b"content_b")

        # First POV discovers cpv_0 at t+10
        hash1, _ = store.add_pov(
            pov1,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            timestamp=crs_start + 10.0,
        )

        # Second POV discovers cpv_1 at t+20 (also triggers cpv_0 but shouldn't override)
        hash2, _ = store.add_pov(
            pov2,
            PovVerificationStatus.CPV,
            ["cpv_0", "cpv_1"],
            timestamp=crs_start + 20.0,
        )

        # cpv_0 should keep first discovery time
        assert store.cpv_to_first_pov["cpv_0"]["pov_hash"] == hash1
        assert store.cpv_to_first_pov["cpv_0"]["relative_time"] == 10.0

        # cpv_1 should have second discovery time
        assert store.cpv_to_first_pov["cpv_1"]["pov_hash"] == hash2
        assert store.cpv_to_first_pov["cpv_1"]["relative_time"] == 20.0


class TestPOVStoreAsyncHelpers:
    """Tests for POVStore async mode helpers (mark_hash_tested, add_pov_by_id)."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> POVStore:
        return POVStore(tmp_path / "povs")

    def test_mark_hash_tested_creates_placeholder(self, store: POVStore) -> None:
        """mark_hash_tested creates an entry so the hash is considered tested."""
        store.mark_hash_tested("abc123")

        assert "abc123" in store.povs
        assert store.povs["abc123"].status == PovVerificationStatus.ERROR
        assert store.povs["abc123"].cpv_matched == []

    def test_mark_hash_tested_prevents_duplicate_enqueue(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """After mark_hash_tested, the POV is seen as already tested."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash = compute_content_hash(pov)

        store.mark_hash_tested(pov_hash)
        assert store.is_already_tested(pov)

    def test_mark_hash_tested_does_not_overwrite_existing(
        self, store: POVStore, tmp_path: Path
    ) -> None:
        """mark_hash_tested should not overwrite an existing real entry."""
        pov = tmp_path / "test.pov"
        pov.write_bytes(b"test content")
        pov_hash, _ = store.add_pov(pov, PovVerificationStatus.CPV, ["cpv_0"])

        # Marking again should not overwrite
        store.mark_hash_tested(pov_hash)
        assert store.povs[pov_hash].status == PovVerificationStatus.CPV
        assert store.povs[pov_hash].cpv_matched == ["cpv_0"]

    def test_add_pov_by_id_creates_entry(self, store: POVStore) -> None:
        """add_pov_by_id creates an entry using the ID as hash key."""
        store.add_pov_by_id("pov_abc.blob", PovVerificationStatus.CPV, ["cpv_0"])

        assert "pov_abc.blob" in store.povs
        assert store.povs["pov_abc.blob"].status == PovVerificationStatus.CPV
        assert store.povs["pov_abc.blob"].cpv_matched == ["cpv_0"]

    def test_add_pov_by_id_tracks_cpv_discovery(self, store: POVStore) -> None:
        """add_pov_by_id registers CPV discovery in cpv_to_first_pov."""
        store.add_pov_by_id("pov1.blob", PovVerificationStatus.CPV, ["cpv_0", "cpv_1"])

        assert "cpv_0" in store.cpv_to_first_pov
        assert "cpv_1" in store.cpv_to_first_pov
        assert store.cpv_to_first_pov["cpv_0"]["pov_hash"] == "pov1.blob"

    def test_add_pov_by_id_updates_placeholder(self, store: POVStore) -> None:
        """add_pov_by_id can update a placeholder created by mark_hash_tested."""
        store.mark_hash_tested("pov1.blob")
        assert store.povs["pov1.blob"].status == PovVerificationStatus.ERROR

        store.add_pov_by_id("pov1.blob", PovVerificationStatus.CPV, ["cpv_0"])
        assert store.povs["pov1.blob"].status == PovVerificationStatus.CPV

    def test_add_pov_by_id_not_vulnerable(self, store: POVStore) -> None:
        """add_pov_by_id with NOT_VULNERABLE does not track CPV discovery."""
        store.add_pov_by_id("pov_miss.blob", PovVerificationStatus.NOT_VULNERABLE, [])

        assert "pov_miss.blob" in store.povs
        assert len(store.cpv_to_first_pov) == 0
