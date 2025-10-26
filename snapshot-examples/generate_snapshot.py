#!/usr/bin/env python3
"""
Generate, list, and validate sample snapshots for testing and documentation.

This script creates realistic snapshot archives that demonstrate the CRSBench
snapshot format, including:
- Incremental POV/patch/corpus capture
- Full LLM usage logs and CRS logs
- Proper tar.gz compression
- Completion markers

Usage:
    # Generate snapshots
    python generate_snapshot.py [output_dir]

    # List all snapshots in a directory with summaries
    python generate_snapshot.py --list [snapshot_dir]

    # List detailed contents of a specific snapshot
    python generate_snapshot.py --list-snapshot <snapshot.tar.gz>

    # Validate snapshots
    python generate_snapshot.py --validate [snapshot_dir]

The script generates snapshots for a simulated trial showing POV discovery
and patch generation over time.
"""

import json
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple
import sys


class SnapshotGenerator:
    """Generate sample snapshot archives for testing and documentation."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Track what has been captured (simulates incremental tracking)
        self.captured_povs = set()
        self.captured_patches = set()
        self.last_corpus_time = 0.0

        # Simulation state
        self.trial_start_time = time.time()
        self.cycle = 0

    def generate_all_snapshots(self):
        """Generate complete set of sample snapshots."""
        print(f"Generating sample snapshots in {self.output_dir}")

        # Snapshot 1: 15 minutes - Initial POV discoveries
        self.generate_snapshot(
            cycle=1,
            elapsed_time=900,  # 15 minutes
            new_povs=["pov_001", "pov_002"],
            new_patches={"pov_0": ["patch.diff"]},
            new_corpus=["input-001", "input-002"],
            llm_calls=50,
            llm_tokens=25000
        )

        # Snapshot 2: 30 minutes - More POVs and patches
        self.generate_snapshot(
            cycle=2,
            elapsed_time=1800,  # 30 minutes
            new_povs=["pov_003"],
            new_patches={"pov_1": ["patch.diff"], "pov_2": ["patch.diff"]},
            new_corpus=["input-003", "input-004", "input-005"],
            llm_calls=95,
            llm_tokens=47500
        )

        # Snapshot 3: 45 minutes - Additional discoveries
        self.generate_snapshot(
            cycle=3,
            elapsed_time=2700,  # 45 minutes
            new_povs=["pov_004", "pov_005"],
            new_patches={"pov_3": ["patch.diff"]},
            new_corpus=["input-006"],
            llm_calls=130,
            llm_tokens=65000
        )

        print(f"\nGenerated 3 snapshots successfully!")
        print(f"Output directory: {self.output_dir}")

    def generate_snapshot(
        self,
        cycle: int,
        elapsed_time: float,
        new_povs: List[str],
        new_patches: Dict[str, List[str]],
        new_corpus: List[str],
        llm_calls: int,
        llm_tokens: int
    ):
        """Generate a single snapshot archive."""
        self.cycle = cycle
        print(f"\nGenerating snapshot {cycle:04d} (elapsed: {elapsed_time}s)")

        # Create temp directory for snapshot contents
        temp_dir = self.output_dir / f".snapshot-{cycle:04d}"
        temp_dir.mkdir(exist_ok=True)

        try:
            # 1. Generate metadata
            self._write_metadata(temp_dir, cycle, elapsed_time)

            # 2. Generate incremental POVs (only new ones)
            if new_povs:
                self._write_povs(temp_dir, new_povs)
                self.captured_povs.update(new_povs)

            # 3. Generate incremental patches (only new ones)
            if new_patches:
                self._write_patches(temp_dir, new_patches)
                for pov_id, patches in new_patches.items():
                    self.captured_patches.update([f"{pov_id}/{p}" for p in patches])

            # 4. Generate incremental corpus (only new files)
            if new_corpus:
                self._write_corpus(temp_dir, new_corpus)

            # 5. Generate full config
            self._write_config(temp_dir)

            # 6. Generate full execution metadata
            self._write_execution_metadata(temp_dir)

            # 7. Generate full LLM usage log
            self._write_llm_usage(temp_dir, llm_calls, llm_tokens)

            # 8. Generate full CRS log
            self._write_crs_log(temp_dir, elapsed_time)

            # 9. Compress to tar.gz
            archive_path = self.output_dir / f"snapshot-{cycle:04d}.tar.gz"
            self._create_tar_gz(temp_dir, archive_path)

            # 10. Create completion marker
            marker_path = self.output_dir / f"snapshot-{cycle:04d}.complete"
            marker_path.touch()

            print(f"  ✓ Created {archive_path.name} ({len(new_povs)} new POVs, "
                  f"{sum(len(p) for p in new_patches.values())} new patches)")

        finally:
            # Cleanup temp directory
            if temp_dir.exists():
                for file in temp_dir.rglob('*'):
                    if file.is_file():
                        file.unlink()
                for dir in sorted(temp_dir.rglob('*'), reverse=True):
                    if dir.is_dir():
                        dir.rmdir()
                temp_dir.rmdir()

    def _write_metadata(self, temp_dir: Path, cycle: int, elapsed_time: float):
        """Write snapshot metadata."""
        metadata = {
            "trial_id": "trial-001",  # Links snapshot to trial
            "cycle": cycle,
            "timestamp": time.time(),
            "elapsed_time": elapsed_time,
            "snapshot_period": 900
        }

        with open(temp_dir / "metadata.json", 'w') as f:
            json.dump(metadata, f, indent=2)

    def _write_povs(self, temp_dir: Path, new_povs: List[str]):
        """Write POV blobs (incremental - only new POVs)."""
        pov_dir = temp_dir / "povs"
        pov_dir.mkdir(exist_ok=True)

        for pov_name in new_povs:
            # Generate binary blob (simulated crash-triggering input)
            blob_data = bytes([i % 256 for i in range(256)])
            (pov_dir / pov_name).write_bytes(blob_data)

    def _write_patches(self, temp_dir: Path, new_patches: Dict[str, List[str]]):
        """Write patch files (incremental - only new patches, organized by POV ID)."""
        patches_dir = temp_dir / "patches"
        patches_dir.mkdir(exist_ok=True)

        for pov_id, patch_files in new_patches.items():
            pov_patch_dir = patches_dir / pov_id
            pov_patch_dir.mkdir(exist_ok=True)

            for patch_file in patch_files:
                # Generate sample patch
                patch_content = f"""--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {{
-    char buffer[256];
+    char buffer[512];
     memcpy(buffer, input, len);
 }}
"""
                (pov_patch_dir / patch_file).write_text(patch_content)

    def _write_corpus(self, temp_dir: Path, new_corpus: List[str]):
        """Write corpus files (incremental - only new/modified files)."""
        corpus_dir = temp_dir / "corpus"
        corpus_dir.mkdir(exist_ok=True)

        for corpus_file in new_corpus:
            # Generate binary test input
            data = bytes([i % 256 for i in range(64)])
            (corpus_dir / corpus_file).write_bytes(data)

    def _write_config(self, temp_dir: Path):
        """Write experiment config (full - static)."""
        config = {
            "experiment": "test-experiment",
            "trials": 3,
            "max_total_time": 7200,
            "snapshot_period": 900,
            "difficulty_level": 1,
            "experiment_filestore": "/tmp/experiments",
            "report_filestore": "/tmp/reports",
            "crses": ["atlantis-c"],
            "benchmark_suite": "crsbench-afc-c"
        }

        with open(temp_dir / "config.yaml", 'w') as f:
            # Simple YAML without dependencies
            for key, value in config.items():
                if isinstance(value, list):
                    f.write(f"{key}:\n")
                    for item in value:
                        f.write(f"  - {item}\n")
                else:
                    f.write(f"{key}: {value}\n")

    def _write_execution_metadata(self, temp_dir: Path):
        """Write execution metadata (full - static)."""
        execution = {
            "trial_id": "trial-001",
            "benchmark": "libjpeg-turbo",
            "crs": "atlantis-c",
            "mode": "bug-finding",
            "started_at": datetime.now().isoformat(),
            "timeout": 7200,
            "docker_image": "gcr.io/oss-fuzz-base/atlantis-c:latest"
        }

        with open(temp_dir / "execution.json", 'w') as f:
            json.dump(execution, f, indent=2)

    def _write_llm_usage(self, temp_dir: Path, total_calls: int, total_tokens: int):
        """Write LLM usage log (full - cumulative)."""
        llm_usage = {
            "total_api_calls": total_calls,
            "total_input_tokens": int(total_tokens * 0.7),
            "total_output_tokens": int(total_tokens * 0.3),
            "total_cached_tokens": int(total_tokens * 0.4),
            "total_cost_usd": round(total_tokens * 0.00003, 4),
            "by_model": {
                "claude-sonnet-4": {
                    "calls": int(total_calls * 0.6),
                    "input_tokens": int(total_tokens * 0.42),
                    "output_tokens": int(total_tokens * 0.18),
                    "cost_usd": round(total_tokens * 0.00002, 4)
                },
                "gpt-4": {
                    "calls": int(total_calls * 0.4),
                    "input_tokens": int(total_tokens * 0.28),
                    "output_tokens": int(total_tokens * 0.12),
                    "cost_usd": round(total_tokens * 0.00001, 4)
                }
            },
            "by_operation": {
                "fuzzing": {"calls": int(total_calls * 0.5), "tokens": int(total_tokens * 0.5)},
                "static_analysis": {"calls": int(total_calls * 0.3), "tokens": int(total_tokens * 0.3)},
                "patch_generation": {"calls": int(total_calls * 0.2), "tokens": int(total_tokens * 0.2)}
            }
        }

        with open(temp_dir / "llm-usage.json", 'w') as f:
            json.dump(llm_usage, f, indent=2)

    def _write_crs_log(self, temp_dir: Path, elapsed_time: float):
        """Write CRS log (full - complete log from start)."""
        log_lines = [
            "[2025-01-15 10:00:00] INFO: CRS starting up",
            "[2025-01-15 10:00:05] INFO: Initializing fuzzing engine",
            "[2025-01-15 10:00:10] INFO: Loading target: libjpeg_djpeg_fuzzer",
            "[2025-01-15 10:05:00] INFO: Starting fuzzing campaign",
        ]

        # Add entries based on POVs discovered so far
        for i, pov_name in enumerate(sorted(self.captured_povs)):
            minutes = 10 + (i * 5)
            log_lines.extend([
                f"[2025-01-15 10:{minutes:02d}:00] INFO: Generated 1000 test cases",
                f"[2025-01-15 10:{minutes:02d}:15] INFO: Found crash: heap-buffer-overflow",
                f"[2025-01-15 10:{minutes:02d}:30] INFO: Analyzing crash with LLM",
                f"[2025-01-15 10:{minutes:02d}:45] INFO: Generated POV candidate {pov_name}",
            ])

        log_lines.append(f"[2025-01-15 10:50:00] INFO: Elapsed time: {elapsed_time}s")
        log_lines.append("[2025-01-15 10:50:00] INFO: Continuing fuzzing campaign...")

        with open(temp_dir / "crs-output.log", 'w') as f:
            f.write('\n'.join(log_lines) + '\n')

    def _create_tar_gz(self, source_dir: Path, archive_path: Path):
        """Compress snapshot directory to tar.gz."""
        with tarfile.open(archive_path, 'w:gz') as tar:
            for item in source_dir.rglob('*'):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=arcname)


class SnapshotLister:
    """List snapshot contents and provide summaries."""

    def __init__(self):
        pass

    def list_directory(self, snapshot_dir: Path):
        """List all snapshots in a directory with summaries."""
        print(f"Snapshots in {snapshot_dir}:\n")

        # Find all snapshot archives
        snapshot_archives = sorted(snapshot_dir.glob("snapshot-*.tar.gz"))

        if not snapshot_archives:
            print("No snapshot archives found.")
            return

        for archive in snapshot_archives:
            self._list_snapshot_summary(archive)
            print()

    def list_snapshot(self, archive_path: Path):
        """List detailed contents of a single snapshot."""
        if not archive_path.exists():
            print(f"Error: Snapshot not found: {archive_path}")
            return

        print(f"Snapshot: {archive_path.name}\n")

        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                members = tar.getmembers()

                # Read and display metadata if present
                try:
                    metadata_member = tar.getmember("metadata.json")
                    metadata_content = tar.extractfile(metadata_member).read()
                    metadata = json.loads(metadata_content)

                    print("Metadata:")
                    print(f"  Cycle: {metadata.get('cycle', 'N/A')}")

                    timestamp = metadata.get('timestamp')
                    if timestamp:
                        dt = datetime.fromtimestamp(timestamp)
                        print(f"  Timestamp: {dt.strftime('%Y-%m-%d %H:%M:%S')}")

                    elapsed = metadata.get('elapsed_time', 0)
                    print(f"  Elapsed: {elapsed}s ({self._format_duration(elapsed)})")
                    print(f"  Period: {metadata.get('snapshot_period', 'N/A')}s")
                    print()
                except:
                    pass

                # List all files with sizes
                print(f"Files ({len(members)} total):")
                self._list_files_tree(members)

        except Exception as e:
            print(f"Error reading snapshot: {e}")

    def _list_snapshot_summary(self, archive_path: Path):
        """Print a summary of a single snapshot."""
        # Extract cycle number
        name_without_gz = archive_path.name.replace('.gz', '')
        cycle = int(Path(name_without_gz).stem.split('-')[1])

        # Get file size
        size = archive_path.stat().st_size
        size_str = self._format_size(size)

        print(f"[Snapshot {cycle:04d}] {archive_path.name} ({size_str})")

        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                members = tar.getmembers()
                member_names = [m.name for m in members]

                # Read metadata
                try:
                    metadata_member = tar.getmember("metadata.json")
                    metadata_content = tar.extractfile(metadata_member).read()
                    metadata = json.loads(metadata_content)

                    elapsed = metadata.get('elapsed_time', 0)
                    print(f"  Cycle: {metadata.get('cycle', 'N/A')}, Elapsed: {elapsed}s ({self._format_duration(elapsed)})")
                except:
                    pass

                print(f"  Files: {len(members)} total")

                # Count POVs
                pov_files = [n for n in member_names if n.startswith("povs/") and n != "povs/"]
                if pov_files:
                    pov_names = [Path(p).name for p in pov_files]
                    print(f"    - POVs: {len(pov_files)} ({', '.join(sorted(pov_names))})")

                # Count patches
                patch_files = [n for n in member_names if n.endswith(".diff")]
                if patch_files:
                    patch_paths = [str(Path(p).parent) for p in patch_files]
                    print(f"    - Patches: {len(patch_files)} ({', '.join(sorted(set(patch_paths)))})")

                # Count corpus
                corpus_files = [n for n in member_names if n.startswith("corpus/") and n != "corpus/"]
                if corpus_files:
                    print(f"    - Corpus: {len(corpus_files)} files")

                # List config files
                config_files = [n for n in member_names if n in ["config.yaml", "execution.json"]]
                if config_files:
                    print(f"    - Config: {', '.join(config_files)}")

                # List log files
                log_files = [n for n in member_names if n.endswith(".json") or n.endswith(".log")]
                log_files = [f for f in log_files if f not in config_files and f != "metadata.json"]
                if log_files:
                    print(f"    - Logs: {', '.join(log_files)}")

                # Metadata
                if "metadata.json" in member_names:
                    print(f"    - Metadata: metadata.json")

        except Exception as e:
            print(f"  Error: {e}")

    def _list_files_tree(self, members: List[tarfile.TarInfo]):
        """List files in a tree structure."""
        # Group files by directory
        dirs = {}
        root_files = []

        for member in members:
            if member.isdir():
                continue

            parts = Path(member.name).parts
            if len(parts) == 1:
                # Root file
                root_files.append((member.name, member.size))
            else:
                # File in directory
                dir_name = parts[0]
                if dir_name not in dirs:
                    dirs[dir_name] = []
                rel_path = str(Path(*parts[1:]))
                dirs[dir_name].append((rel_path, member.size))

        # Print root files
        for name, size in sorted(root_files):
            print(f"  {name} ({self._format_size(size)})")

        # Print directories with proper tree structure
        for dir_name in sorted(dirs.keys()):
            print(f"  {dir_name}/")

            # Build tree structure for this directory
            tree_printed = set()  # Track what we've already printed

            for file_path, size in sorted(dirs[dir_name]):
                path_obj = Path(file_path)
                parts = path_obj.parts

                # Print intermediate directories
                for i in range(len(parts) - 1):
                    subdir_path = str(Path(*parts[:i+1]))
                    if subdir_path not in tree_printed:
                        indent = "    " + "  " * i
                        print(f"{indent}{parts[i]}/")
                        tree_printed.add(subdir_path)

                # Print the file
                indent = "    " + "  " * (len(parts) - 1)
                print(f"{indent}{path_obj.name} ({self._format_size(size)})")

    def _format_size(self, size: int) -> str:
        """Format byte size to human-readable string."""
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}K"
        else:
            return f"{size / (1024 * 1024):.1f}M"

    def _format_duration(self, seconds: float) -> str:
        """Format duration in seconds to human-readable string."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.0f}m"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h{minutes}m"


class SnapshotValidator:
    """Validate snapshot format and structure."""

    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = snapshot_dir
        self.errors = []
        self.warnings = []

    def validate_all(self) -> bool:
        """Validate all snapshots in directory."""
        print(f"Validating snapshots in {self.snapshot_dir}")

        # Find all snapshot archives
        snapshot_archives = sorted(self.snapshot_dir.glob("snapshot-*.tar.gz"))

        if not snapshot_archives:
            self.errors.append("No snapshot archives found")
            return False

        print(f"Found {len(snapshot_archives)} snapshot archives")

        all_valid = True
        for archive in snapshot_archives:
            if not self.validate_snapshot(archive):
                all_valid = False

        return all_valid

    def validate_snapshot(self, archive_path: Path) -> bool:
        """Validate a single snapshot archive."""
        # Extract cycle number from "snapshot-0001.tar.gz" -> "snapshot-0001.tar" -> "0001"
        name_without_gz = archive_path.name.replace('.gz', '')
        cycle = int(Path(name_without_gz).stem.split('-')[1])
        print(f"\n[Snapshot {cycle:04d}] Validating {archive_path.name}")

        valid = True

        # Check completion marker exists
        # For "snapshot-0001.tar.gz", we want "snapshot-0001.complete"
        name_without_gz = archive_path.name.replace('.tar.gz', '')
        marker_path = archive_path.parent / f"{name_without_gz}.complete"
        if not marker_path.exists():
            self.errors.append(f"  ✗ Missing completion marker: {marker_path.name}")
            valid = False
        else:
            print(f"  ✓ Completion marker exists")

        # Check archive can be opened
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                members = tar.getmembers()
                print(f"  ✓ Archive is valid tar.gz ({len(members)} files)")

                # Validate required files
                valid &= self._validate_required_files(tar, members, cycle)

                # Validate file structures
                valid &= self._validate_file_structures(tar, members, cycle)

        except Exception as e:
            self.errors.append(f"  ✗ Failed to open archive: {e}")
            valid = False

        return valid

    def _validate_required_files(self, tar: tarfile.TarFile, members: List, cycle: int) -> bool:
        """Validate required files exist in snapshot."""
        required_files = ["metadata.json", "config.yaml", "execution.json", "llm-usage.json", "crs-output.log"]
        member_names = [m.name for m in members]

        valid = True
        for req_file in required_files:
            if req_file not in member_names:
                self.errors.append(f"  ✗ Missing required file: {req_file}")
                valid = False
            else:
                print(f"  ✓ Found {req_file}")

        return valid

    def _validate_file_structures(self, tar: tarfile.TarFile, members: List, cycle: int) -> bool:
        """Validate file structures and contents."""
        valid = True

        # Validate metadata.json
        try:
            metadata_member = tar.getmember("metadata.json")
            metadata_content = tar.extractfile(metadata_member).read()
            metadata = json.loads(metadata_content)

            if metadata.get("cycle") != cycle:
                self.errors.append(f"  ✗ Metadata cycle mismatch: expected {cycle}, got {metadata.get('cycle')}")
                valid = False
            else:
                print(f"  ✓ Metadata cycle matches: {cycle}")

            required_metadata_keys = ["trial_id", "cycle", "timestamp", "elapsed_time", "snapshot_period"]
            for key in required_metadata_keys:
                if key not in metadata:
                    self.errors.append(f"  ✗ Metadata missing key: {key}")
                    valid = False

            if valid:
                print(f"  ✓ Metadata structure valid")

        except Exception as e:
            self.errors.append(f"  ✗ Failed to validate metadata.json: {e}")
            valid = False

        # Validate LLM usage JSON
        try:
            llm_member = tar.getmember("llm-usage.json")
            llm_content = tar.extractfile(llm_member).read()
            llm_usage = json.loads(llm_content)

            required_llm_keys = ["total_api_calls", "total_input_tokens", "total_output_tokens", "total_cost_usd"]
            for key in required_llm_keys:
                if key not in llm_usage:
                    self.errors.append(f"  ✗ LLM usage missing key: {key}")
                    valid = False

            if valid:
                print(f"  ✓ LLM usage structure valid")

        except Exception as e:
            self.errors.append(f"  ✗ Failed to validate llm-usage.json: {e}")
            valid = False

        # Check for incremental data (POVs, patches, corpus)
        member_names = [m.name for m in members]
        has_povs = any(name.startswith("povs/") for name in member_names)
        has_patches = any(name.startswith("patches/") for name in member_names)
        has_corpus = any(name.startswith("corpus/") for name in member_names)

        if has_povs:
            pov_count = len([n for n in member_names if n.startswith("povs/") and n != "povs/"])
            print(f"  ✓ Contains {pov_count} POV(s)")
        else:
            print(f"  ℹ No POVs in this snapshot (may be normal for early/late snapshots)")

        if has_patches:
            patch_count = len([n for n in member_names if n.endswith(".diff")])
            print(f"  ✓ Contains {patch_count} patch(es)")
        else:
            print(f"  ℹ No patches in this snapshot")

        if has_corpus:
            corpus_count = len([n for n in member_names if n.startswith("corpus/") and n != "corpus/"])
            print(f"  ✓ Contains {corpus_count} corpus file(s)")

        # Validate patch directory structure (patches organized by POV ID)
        if has_patches:
            patch_files = [n for n in member_names if n.startswith("patches/")]
            # Check all patches are in pov_N/ subdirectories
            for patch_file in patch_files:
                if patch_file == "patches/":
                    continue
                parts = Path(patch_file).parts
                if len(parts) < 3:  # Should be patches/pov_N/patch.diff
                    self.warnings.append(f"  ⚠ Patch not in POV subdirectory: {patch_file}")
                elif not parts[1].startswith("pov_"):
                    self.warnings.append(f"  ⚠ Patch directory doesn't follow pov_N naming: {parts[1]}")

        return valid

    def print_summary(self, success: bool):
        """Print validation summary."""
        print("\n" + "="*60)
        if success:
            print("✓ All snapshots are valid!")
        else:
            print("✗ Snapshot validation FAILED")
        print("="*60)

        if self.errors:
            print(f"\nErrors ({len(self.errors)}):")
            for error in self.errors:
                print(error)

        if self.warnings:
            print(f"\nWarnings ({len(self.warnings)}):")
            for warning in self.warnings:
                print(warning)


def main():
    """Main entry point."""
    # Check for list directory mode
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        if len(sys.argv) > 2:
            snapshot_dir = Path(sys.argv[2])
        else:
            snapshot_dir = Path(__file__).parent / "trial-example"

        lister = SnapshotLister()
        lister.list_directory(snapshot_dir)
        sys.exit(0)

    # Check for list single snapshot mode
    if len(sys.argv) > 1 and sys.argv[1] == "--list-snapshot":
        if len(sys.argv) < 3:
            print("Error: --list-snapshot requires a snapshot file path")
            print(f"Usage: python {Path(__file__).name} --list-snapshot <snapshot.tar.gz>")
            sys.exit(1)

        snapshot_file = Path(sys.argv[2])
        lister = SnapshotLister()
        lister.list_snapshot(snapshot_file)
        sys.exit(0)

    # Check for validation mode
    if len(sys.argv) > 1 and sys.argv[1] == "--validate":
        if len(sys.argv) > 2:
            snapshot_dir = Path(sys.argv[2])
        else:
            snapshot_dir = Path(__file__).parent / "trial-example"

        validator = SnapshotValidator(snapshot_dir)
        success = validator.validate_all()
        validator.print_summary(success)
        sys.exit(0 if success else 1)

    # Generation mode
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "trial-example"

    generator = SnapshotGenerator(output_dir)
    generator.generate_all_snapshots()

    print("\n" + "="*60)
    print("Sample snapshots generated successfully!")
    print("="*60)
    print(f"\nLocation: {output_dir}")
    print("\nTo list snapshots:")
    print(f"  python {Path(__file__).name} --list {output_dir}")
    print(f"  python {Path(__file__).name} --list-snapshot {output_dir}/snapshot-0001.tar.gz")
    print("\nTo validate snapshots:")
    print(f"  python {Path(__file__).name} --validate {output_dir}")


if __name__ == "__main__":
    main()
