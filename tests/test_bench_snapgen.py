"""Tests for bench_snapgen module."""

import json
import tarfile
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from crsbench.bench_snapgen.generator import (
    BenchmarkSnapshotGenerator,
    BenchmarkData,
    POVData,
    load_benchmark_ground_truth,
)
from crsbench.bench_snapgen.timeline import (
    DiscoveryEvent,
    DiscoveryTimeline,
    POVDiscoveryModel,
    PatchGenerationModel,
    create_discovery_timeline,
)
from crsbench.bench_snapgen.fault_injection import (
    FaultInjector,
    inject_faults_into_timeline,
)
from crsbench.bench_snapgen.builder import SnapshotBuilder
from crsbench.validation.schemas import BenchmarkConfig, HarnessFile, Vulnerability, POV


class TestBenchmarkReading:
    """Test benchmark ground truth loading."""

    def test_load_benchmark_ground_truth_from_real_benchmark(self):
        """Test loading from actual benchmark directory."""
        # Find a real benchmark
        benchmarks_dir = Path("benchmarks")
        if not benchmarks_dir.exists():
            pytest.skip("benchmarks/ directory not found")

        # Try to find afc-curl-delta-01 or any benchmark with .aixcc/
        benchmark_path = benchmarks_dir / "afc-curl-delta-01"
        if not benchmark_path.exists():
            # Try to find any benchmark
            candidates = [
                b
                for b in benchmarks_dir.iterdir()
                if b.is_dir() and (b / ".aixcc").exists()
            ]
            if not candidates:
                pytest.skip("No benchmarks with .aixcc/ found")
            benchmark_path = candidates[0]

        # Load ground truth
        data = load_benchmark_ground_truth(benchmark_path)

        # Verify structure
        assert isinstance(data, BenchmarkData)
        assert isinstance(data.meta, BenchmarkConfig)
        assert isinstance(data.povs, dict)
        assert isinstance(data.patches, dict)

        # Should have at least some POVs
        assert len(data.povs) > 0, "Benchmark should have at least one POV"

        # Verify POV data structure
        for pov_key, pov_data in data.povs.items():
            assert isinstance(pov_key, tuple)
            assert len(pov_key) == 3  # (harness, vuln, pov_id)
            assert isinstance(pov_data, POVData)
            assert isinstance(pov_data.blob, bytes)
            assert len(pov_data.blob) > 0

    def test_load_benchmark_nonexistent_path(self):
        """Test loading from nonexistent path fails."""
        with pytest.raises(FileNotFoundError):
            load_benchmark_ground_truth(Path("/nonexistent/path"))

    def test_load_benchmark_no_aixcc_dir(self, tmp_path):
        """Test loading from path without .aixcc/ fails."""
        benchmark_dir = tmp_path / "test-benchmark"
        benchmark_dir.mkdir()

        with pytest.raises(FileNotFoundError, match=".aixcc"):
            load_benchmark_ground_truth(benchmark_dir)


class TestDiscoveryTimeline:
    """Test discovery timeline functionality."""

    def test_create_empty_timeline(self):
        """Test creating empty timeline."""
        timeline = DiscoveryTimeline()
        assert timeline.events == []

    def test_add_pov_to_timeline(self):
        """Test adding POV event."""
        timeline = DiscoveryTimeline()
        pov_blob = b"test pov data"

        timeline.add_pov(
            time=100.0,
            pov_blob=pov_blob,
            harness="test_harness",
            vuln="cpv_0",
            pov_id="pov_0",
            sanitizer="address",
        )

        assert len(timeline.events) == 1
        event = timeline.events[0]
        assert event.event_type == "pov"
        assert event.timestamp == 100.0
        assert event.data == pov_blob
        assert event.metadata["pov_id"] == "pov_0"
        assert event.is_valid is True

    def test_add_patch_to_timeline(self):
        """Test adding patch event."""
        timeline = DiscoveryTimeline()
        patch_content = "--- a/file.c\n+++ b/file.c\n"

        timeline.add_patch(
            time=200.0,
            patch_diff=patch_content,
            harness="test_harness",
            vuln="cpv_0",
            patch_id="patch_0",
        )

        assert len(timeline.events) == 1
        event = timeline.events[0]
        assert event.event_type == "patch"
        assert event.timestamp == 200.0
        assert event.data.decode("utf-8") == patch_content
        assert event.metadata["patch_id"] == "patch_0"

    def test_timeline_sorting(self):
        """Test events are sorted by timestamp."""
        timeline = DiscoveryTimeline()

        # Add events out of order
        timeline.add_pov(
            time=300.0,
            pov_blob=b"third",
            harness="h",
            vuln="v",
            pov_id="pov_2",
        )
        timeline.add_pov(
            time=100.0,
            pov_blob=b"first",
            harness="h",
            vuln="v",
            pov_id="pov_0",
        )
        timeline.add_pov(
            time=200.0,
            pov_blob=b"second",
            harness="h",
            vuln="v",
            pov_id="pov_1",
        )

        # Should be sorted by timestamp
        assert len(timeline.events) == 3
        assert timeline.events[0].timestamp == 100.0
        assert timeline.events[1].timestamp == 200.0
        assert timeline.events[2].timestamp == 300.0

    def test_get_events_before(self):
        """Test filtering events by time."""
        timeline = DiscoveryTimeline()

        timeline.add_pov(
            time=100.0, pov_blob=b"1", harness="h", vuln="v", pov_id="pov_0"
        )
        timeline.add_pov(
            time=200.0, pov_blob=b"2", harness="h", vuln="v", pov_id="pov_1"
        )
        timeline.add_pov(
            time=300.0, pov_blob=b"3", harness="h", vuln="v", pov_id="pov_2"
        )

        # Get events before t=200
        events = timeline.get_events_before(200.0)
        assert len(events) == 2
        assert events[0].timestamp == 100.0
        assert events[1].timestamp == 200.0

        # Get all events
        all_events = timeline.get_events_before(1000.0)
        assert len(all_events) == 3


class TestPOVDiscoveryModel:
    """Test POV discovery timing model."""

    def test_difficulty_based_timing(self):
        """Test POV discovery varies by difficulty."""
        model = POVDiscoveryModel()
        max_time = 7200.0

        # Test each difficulty level
        for difficulty in range(1, 6):
            times = model.get_discovery_times(difficulty, pov_count=1, max_time=max_time)

            assert len(times) == 1

            # Check time is within expected range for difficulty
            min_pct, max_pct = model.DIFFICULTY_TIMING[difficulty]
            expected_min = max_time * min_pct
            expected_max = max_time * max_pct

            assert (
                expected_min <= times[0] <= expected_max
            ), f"Difficulty {difficulty}: time {times[0]} not in range [{expected_min}, {expected_max}]"

    def test_pov_clustering(self):
        """Test multiple POVs clustered together."""
        model = POVDiscoveryModel()
        max_time = 7200.0
        pov_count = 5

        times = model.get_discovery_times(difficulty=2, pov_count=pov_count, max_time=max_time)

        assert len(times) == pov_count

        # Times should be clustered (within ~10% of each other)
        time_range = max(times) - min(times)
        assert time_range < max_time * 0.2, "POVs should be clustered together"

    def test_times_within_bounds(self):
        """Test all times are within [0, max_time]."""
        model = POVDiscoveryModel()
        max_time = 7200.0

        for difficulty in range(1, 6):
            times = model.get_discovery_times(difficulty, pov_count=10, max_time=max_time)

            for time in times:
                assert 0.0 <= time <= max_time

    def test_invalid_difficulty(self):
        """Test invalid difficulty raises error."""
        model = POVDiscoveryModel()

        with pytest.raises(ValueError, match="Invalid difficulty"):
            model.get_discovery_times(difficulty=0, pov_count=1, max_time=7200.0)

        with pytest.raises(ValueError, match="Invalid difficulty"):
            model.get_discovery_times(difficulty=6, pov_count=1, max_time=7200.0)


class TestPatchGenerationModel:
    """Test patch generation timing model."""

    def test_patch_after_pov(self):
        """Test patch comes after POV with realistic delay."""
        model = PatchGenerationModel()
        first_pov_time = 1000.0
        max_time = 7200.0

        for difficulty in range(1, 6):
            patch_time = model.get_patch_time(first_pov_time, difficulty, max_time)

            # Patch should be after POV
            assert patch_time > first_pov_time

            # Patch should be within delay range
            min_delay, max_delay = model.DIFFICULTY_DELAYS[difficulty]
            assert first_pov_time + min_delay <= patch_time <= max_time

    def test_patch_bounded_by_max_time(self):
        """Test patch time doesn't exceed max_time."""
        model = PatchGenerationModel()

        # POV discovered very late
        first_pov_time = 7000.0
        max_time = 7200.0

        patch_time = model.get_patch_time(first_pov_time, difficulty=1, max_time=max_time)

        # Should be capped at max_time
        assert patch_time <= max_time


class TestFaultInjection:
    """Test fault injection functionality."""

    def test_create_invalid_pov(self):
        """Test invalid POV generation."""
        injector = FaultInjector(fault_rate=0.5)

        blob, metadata = injector.create_invalid_pov()

        assert isinstance(blob, bytes)
        assert len(blob) > 0
        assert metadata["pov_id"].startswith("invalid_pov_")
        assert metadata["fault_type"] == "invalid_pov"

    def test_create_invalid_patches(self):
        """Test all invalid patch types."""
        injector = FaultInjector(fault_rate=0.5)

        fault_types = ["syntax_error", "wrong_file", "incomplete", "breaks_build"]

        for fault_type in fault_types:
            patch_content, metadata = injector.create_invalid_patch(fault_type)

            assert isinstance(patch_content, str)
            assert len(patch_content) > 0
            assert metadata["patch_id"].startswith("invalid_patch_")
            assert metadata["fault_type"] == fault_type

    def test_invalid_fault_type_raises_error(self):
        """Test invalid fault_type raises error."""
        injector = FaultInjector()

        with pytest.raises(ValueError, match="Invalid fault_type"):
            injector.create_invalid_patch("nonexistent_type")

    def test_inject_faults_into_timeline(self):
        """Test fault injection into timeline."""
        timeline = DiscoveryTimeline()

        # Add some valid events
        for i in range(10):
            timeline.add_pov(
                time=float(i * 100),
                pov_blob=b"valid",
                harness="h",
                vuln="v",
                pov_id=f"pov_{i}",
            )

        original_count = len(timeline.events)

        # Inject faults (10% rate)
        injector = FaultInjector(fault_rate=0.1)
        inject_faults_into_timeline(timeline, injector, max_time=1000.0)

        # Should have approximately 10% more events
        assert len(timeline.events) > original_count

        # Check for invalid events
        invalid_events = [e for e in timeline.events if not e.is_valid]
        assert len(invalid_events) > 0

    def test_zero_fault_rate_no_injection(self):
        """Test zero fault rate doesn't inject faults."""
        timeline = DiscoveryTimeline()

        timeline.add_pov(
            time=100.0, pov_blob=b"valid", harness="h", vuln="v", pov_id="pov_0"
        )

        original_count = len(timeline.events)

        injector = FaultInjector(fault_rate=0.0)
        inject_faults_into_timeline(timeline, injector, max_time=1000.0)

        # No new events added
        assert len(timeline.events) == original_count


class TestSnapshotBuilder:
    """Test snapshot builder functionality."""

    def test_create_snapshot_archive(self, tmp_path):
        """Test snapshot archive creation."""
        timeline = DiscoveryTimeline()

        # Add some events
        timeline.add_pov(
            time=100.0,
            pov_blob=b"test pov data",
            harness="test_harness",
            vuln="cpv_0",
            pov_id="pov_0",
            sanitizer="address",
        )

        builder = SnapshotBuilder(tmp_path)
        builder.snapshot_period = 900

        # Build snapshot
        archive_path = builder.build_snapshot(
            cycle=1,
            elapsed_time=900.0,
            timeline=timeline,
            benchmark_name="test-benchmark",
            crs_name="test-crs",
        )

        # Verify archive exists
        assert archive_path.exists()
        assert archive_path.name == "snapshot-0001.tar.gz"

        # Verify completion marker
        marker_path = tmp_path / "snapshot-0001.complete"
        assert marker_path.exists()

        # Verify archive contents
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            member_names = [m.name for m in members]

            # Check required files
            assert "metadata.json" in member_names
            assert "config.yaml" in member_names
            assert "execution.json" in member_names
            assert "llm-usage.json" in member_names
            assert "crs-output.log" in member_names

            # Check POV was captured
            assert "povs/pov_0" in member_names

    def test_incremental_pov_tracking(self, tmp_path):
        """Test POVs only written once."""
        timeline = DiscoveryTimeline()

        # Add POVs
        timeline.add_pov(
            time=100.0, pov_blob=b"pov1", harness="h", vuln="v", pov_id="pov_0"
        )
        timeline.add_pov(
            time=200.0, pov_blob=b"pov2", harness="h", vuln="v", pov_id="pov_1"
        )
        timeline.add_pov(
            time=300.0, pov_blob=b"pov3", harness="h", vuln="v", pov_id="pov_2"
        )

        builder = SnapshotBuilder(tmp_path)

        # Snapshot 1: should capture pov_0
        builder.build_snapshot(
            cycle=1,
            elapsed_time=150.0,
            timeline=timeline,
        )

        # Snapshot 2: should capture pov_1 only (pov_0 already captured)
        archive2 = builder.build_snapshot(
            cycle=2,
            elapsed_time=250.0,
            timeline=timeline,
        )

        # Check snapshot 2 only has pov_1
        with tarfile.open(archive2, "r:gz") as tar:
            pov_members = [m for m in tar.getmembers() if m.name.startswith("povs/")]
            pov_names = [m.name for m in pov_members]

            assert "povs/pov_1" in pov_names
            assert "povs/pov_0" not in pov_names  # Not in this snapshot


class TestGeneratorIntegration:
    """Integration tests with real benchmarks."""

    def test_generate_snapshots_bug_finding_mode(self, tmp_path):
        """Test complete snapshot generation in bug-finding mode."""
        benchmarks_dir = Path("benchmarks")
        if not benchmarks_dir.exists():
            pytest.skip("benchmarks/ directory not found")

        # Find a benchmark
        benchmark_path = benchmarks_dir / "afc-curl-delta-01"
        if not benchmark_path.exists():
            candidates = [
                b
                for b in benchmarks_dir.iterdir()
                if b.is_dir() and (b / ".aixcc").exists()
            ]
            if not candidates:
                pytest.skip("No benchmarks with .aixcc/ found")
            benchmark_path = candidates[0]

        # Load benchmark data to get the first available harness
        from crsbench.bench_snapgen.generator import load_benchmark_ground_truth
        benchmark_data = load_benchmark_ground_truth(benchmark_path)

        # Find first harness with POVs
        harness = None
        for h in benchmark_data.meta.harness_files:
            harness_povs = [k for k in benchmark_data.povs if k[0] == h.name]
            if harness_povs:
                harness = h.name
                break

        if not harness:
            pytest.skip("No harness with POVs found")

        # Generate snapshots
        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_path,
            output_dir=tmp_path,
            trial_duration=1800,  # 30 minutes
            snapshot_period=600,  # 10 minutes
            harness=harness,
        )

        output_dir = generator.generate_trial_snapshots(
            mode="bug-finding", difficulty_level=2
        )

        # Verify output
        assert output_dir == tmp_path
        snapshots = list(tmp_path.glob("snapshot-*.tar.gz"))
        assert len(snapshots) == 3  # 1800 / 600 = 3

        # Verify each snapshot
        for snapshot in snapshots:
            assert snapshot.exists()
            # snapshot.stem is "snapshot-0001.tar", we want "snapshot-0001"
            # So we need to remove the .tar extension from stem
            snapshot_name = snapshot.name.replace(".tar.gz", "")
            marker = tmp_path / f"{snapshot_name}.complete"
            assert marker.exists()

    def test_generate_snapshots_patch_mode(self, tmp_path):
        """Test snapshot generation in patch-generation mode."""
        benchmarks_dir = Path("benchmarks")
        if not benchmarks_dir.exists():
            pytest.skip("benchmarks/ directory not found")

        # Find a benchmark with patches
        benchmark_path = benchmarks_dir / "afc-curl-delta-01"
        if not benchmark_path.exists():
            pytest.skip("afc-curl-delta-01 not found")

        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_path,
            output_dir=tmp_path,
            trial_duration=900,
            snapshot_period=300,
            harness="curl_fuzzer_ws",
        )

        output_dir = generator.generate_trial_snapshots(
            mode="patch-generation", difficulty_level=1
        )

        # Verify snapshots generated
        snapshots = list(tmp_path.glob("snapshot-*.tar.gz"))
        assert len(snapshots) > 0

    def test_generator_with_fault_injection(self, tmp_path):
        """Test generator with fault injection."""
        benchmarks_dir = Path("benchmarks")
        if not benchmarks_dir.exists():
            pytest.skip("benchmarks/ directory not found")

        benchmark_path = benchmarks_dir / "afc-curl-delta-01"
        if not benchmark_path.exists():
            pytest.skip("afc-curl-delta-01 not found")

        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_path,
            output_dir=tmp_path,
            trial_duration=900,
            snapshot_period=450,
            harness="curl_fuzzer_ws",
        )

        output_dir = generator.generate_trial_snapshots(
            mode="bug-finding",
            difficulty_level=1,
            fault_injection_rate=0.2,  # 20% invalid data
        )

        # Verify snapshots generated
        snapshots = list(tmp_path.glob("snapshot-*.tar.gz"))
        assert len(snapshots) > 0

    def test_generator_invalid_mode(self, tmp_path):
        """Test generator raises error for invalid mode."""
        # Create minimal benchmark structure
        benchmark_dir = tmp_path / "test-benchmark"
        benchmark_dir.mkdir()
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        # Create minimal meta.yaml
        meta_content = """
delta_mode:
  base_commit: a1b2c3d4e5f6
  ref_commit: f6e5d4c3b2a1

harness_files:
  - name: test_harness
    path: /test/harness.c
    vulns:
      - vuln_keyword: cpv_0
        povs:
          - id: pov_0
            sanitizer: address
"""
        (aixcc_dir / "meta.yaml").write_text(meta_content)

        # Create POV
        harness_dir = aixcc_dir / "test_harness"
        harness_dir.mkdir()
        vuln_dir = harness_dir / "cpv_0"
        vuln_dir.mkdir()
        blobs_dir = vuln_dir / "blobs"
        blobs_dir.mkdir()
        (blobs_dir / "pov_0.blob").write_bytes(b"test")

        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_dir,
            output_dir=tmp_path / "output",
            trial_duration=900,
            snapshot_period=300,
            harness="test_harness",
        )

        with pytest.raises(ValueError, match="Invalid mode"):
            generator.generate_trial_snapshots(mode="invalid-mode")

    def test_generator_invalid_difficulty(self, tmp_path):
        """Test generator raises error for invalid difficulty."""
        # Use same minimal benchmark as above
        benchmark_dir = tmp_path / "test-benchmark"
        benchmark_dir.mkdir()
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        meta_content = """
delta_mode:
  base_commit: a1b2c3d4e5f6
  ref_commit: f6e5d4c3b2a1

harness_files:
  - name: test_harness
    path: /test/harness.c
    vulns:
      - vuln_keyword: cpv_0
        povs:
          - id: pov_0
            sanitizer: address
"""
        (aixcc_dir / "meta.yaml").write_text(meta_content)

        harness_dir = aixcc_dir / "test_harness"
        harness_dir.mkdir()
        vuln_dir = harness_dir / "cpv_0"
        vuln_dir.mkdir()
        blobs_dir = vuln_dir / "blobs"
        blobs_dir.mkdir()
        (blobs_dir / "pov_0.blob").write_bytes(b"test")

        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_dir,
            output_dir=tmp_path / "output",
            trial_duration=900,
            snapshot_period=300,
            harness="test_harness",
        )

        with pytest.raises(ValueError, match="difficulty_level"):
            generator.generate_trial_snapshots(difficulty_level=0)

        with pytest.raises(ValueError, match="difficulty_level"):
            generator.generate_trial_snapshots(difficulty_level=6)

    def test_harness_required(self, tmp_path):
        """Test that harness parameter is required."""
        # Create minimal benchmark
        benchmark_dir = tmp_path / "benchmark"
        benchmark_dir.mkdir()
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        meta_content = """
delta_mode:
  base_commit: a1b2c3d4e5f6
  ref_commit: f6e5d4c3b2a1

harness_files:
  - name: test_harness
    path: /test/harness.c
    vulns:
      - vuln_keyword: cpv_0
        difficulty_level: 1
        povs:
          - id: pov_0
            sanitizer: address
"""
        (aixcc_dir / "meta.yaml").write_text(meta_content)

        # Create POV
        harness_dir = aixcc_dir / "test_harness"
        harness_dir.mkdir()
        vuln_dir = harness_dir / "cpv_0"
        vuln_dir.mkdir()
        blobs_dir = vuln_dir / "blobs"
        blobs_dir.mkdir()
        (blobs_dir / "pov_0.blob").write_bytes(b"test")

        # Test that harness is required
        with pytest.raises(ValueError, match="harness parameter is required"):
            BenchmarkSnapshotGenerator(
                benchmark_path=benchmark_dir,
                output_dir=tmp_path / "output",
                trial_duration=900,
                snapshot_period=300,
                harness=None,
            )

    def test_invalid_harness(self, tmp_path):
        """Test that invalid harness raises error."""
        # Create minimal benchmark
        benchmark_dir = tmp_path / "benchmark"
        benchmark_dir.mkdir()
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        meta_content = """
delta_mode:
  base_commit: a1b2c3d4e5f6
  ref_commit: f6e5d4c3b2a1

harness_files:
  - name: test_harness
    path: /test/harness.c
    vulns:
      - vuln_keyword: cpv_0
        difficulty_level: 1
        povs:
          - id: pov_0
            sanitizer: address
"""
        (aixcc_dir / "meta.yaml").write_text(meta_content)

        # Create POV
        harness_dir = aixcc_dir / "test_harness"
        harness_dir.mkdir()
        vuln_dir = harness_dir / "cpv_0"
        vuln_dir.mkdir()
        blobs_dir = vuln_dir / "blobs"
        blobs_dir.mkdir()
        (blobs_dir / "pov_0.blob").write_bytes(b"test")

        # Test that invalid harness raises error
        with pytest.raises(ValueError, match="Harness 'nonexistent' not found or has no POVs"):
            BenchmarkSnapshotGenerator(
                benchmark_path=benchmark_dir,
                output_dir=tmp_path / "output",
                trial_duration=900,
                snapshot_period=300,
                harness="nonexistent",
            )

    def test_final_epoch_guarantee(self, tmp_path):
        """Test that at least one POV appears in final snapshot."""
        # Create minimal benchmark
        benchmark_dir = tmp_path / "benchmark"
        benchmark_dir.mkdir()
        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        meta_content = """
delta_mode:
  base_commit: a1b2c3d4e5f6
  ref_commit: f6e5d4c3b2a1

harness_files:
  - name: test_harness
    path: /test/harness.c
    vulns:
      - vuln_keyword: cpv_0
        difficulty_level: 1
        povs:
          - id: pov_0
            sanitizer: address
"""
        (aixcc_dir / "meta.yaml").write_text(meta_content)

        # Create POV
        harness_dir = aixcc_dir / "test_harness"
        harness_dir.mkdir()
        vuln_dir = harness_dir / "cpv_0"
        vuln_dir.mkdir()
        blobs_dir = vuln_dir / "blobs"
        blobs_dir.mkdir()
        (blobs_dir / "pov_0.blob").write_bytes(b"test POV data")

        # Generate snapshots
        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_dir,
            output_dir=tmp_path / "output",
            trial_duration=1800,  # 30 minutes
            snapshot_period=600,  # 10 minutes
            harness="test_harness",
        )

        generator.generate_trial_snapshots(
            mode="bug-finding", difficulty_level=1
        )

        # Extract final snapshot and verify POV exists
        import tarfile
        snapshots = sorted(tmp_path.glob("output/snapshot-*.tar.gz"))
        final_snapshot = snapshots[-1]

        with tarfile.open(final_snapshot, "r:gz") as tar:
            members = tar.getnames()
            # Check if POV is in final snapshot
            pov_files = [m for m in members if m.startswith("povs/")]
            assert len(pov_files) > 0, "Final snapshot should contain at least one POV"
