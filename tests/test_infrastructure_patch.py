"""Tests for patch parsing and hunk-level deduplication.

Tests:
- parse_patch_into_hunks(): Parse unified diff into PatchFile/PatchHunk objects
- deduplicate_hunks(): Remove duplicate hunks across multiple patches
- write_patch_files_to_diff(): Convert PatchFile back to diff format
- apply_patches_from_list(): Integration test for hunk-level deduplication
"""

from pathlib import Path

import pytest
from crsbench.builder.infrastructure import (
    PatchHunk,
    deduplicate_hunks,
    parse_patch_into_hunks,
    write_patch_files_to_diff,
)

# Sample patch content for testing
SAMPLE_PATCH_SINGLE_FILE = """\
diff --git a/src/util.c b/src/util.c
index abc1234..def5678 100644
--- a/src/util.c
+++ b/src/util.c
@@ -10,6 +10,10 @@ void existing_func() {
     // existing code
 }

+void new_func() {
+    // new function
+}
+
 void another_func() {
     // more code
 }
"""

SAMPLE_PATCH_TWO_FILES = """\
diff --git a/src/util.c b/src/util.c
index abc1234..def5678 100644
--- a/src/util.c
+++ b/src/util.c
@@ -10,6 +10,10 @@ void existing_func() {
     // existing code
 }

+void new_func() {
+    // new function
+}
+
 void another_func() {
     // more code
 }
diff --git a/src/main.c b/src/main.c
index 111222..333444 100644
--- a/src/main.c
+++ b/src/main.c
@@ -5,6 +5,7 @@ int main() {
     init();
+    new_func();
     run();
     return 0;
 }
"""

# Second patch that shares the util.c hunk but has different main.c hunk
SAMPLE_PATCH_OVERLAPPING = """\
diff --git a/src/util.c b/src/util.c
index abc1234..def5678 100644
--- a/src/util.c
+++ b/src/util.c
@@ -10,6 +10,10 @@ void existing_func() {
     // existing code
 }

+void new_func() {
+    // new function
+}
+
 void another_func() {
     // more code
 }
diff --git a/src/other.c b/src/other.c
index aaa111..bbb222 100644
--- a/src/other.c
+++ b/src/other.c
@@ -1,3 +1,4 @@
+#include "util.h"
 void other() {
     // code
 }
"""


class TestParsePatchIntoHunks:
    """Tests for parse_patch_into_hunks()."""

    def test_single_file_single_hunk(self):
        """Parse patch with one file and one hunk."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_SINGLE_FILE)

        assert len(patch_files) == 1
        pf = patch_files[0]
        assert pf.file_path == "src/util.c"
        assert len(pf.hunks) == 1
        assert pf.hunks[0].file_path == "src/util.c"
        assert "@@ -10,6 +10,10 @@" in pf.hunks[0].header

    def test_two_files(self):
        """Parse patch with two files."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_TWO_FILES)

        assert len(patch_files) == 2
        assert patch_files[0].file_path == "src/util.c"
        assert patch_files[1].file_path == "src/main.c"

    def test_hunk_content_preserved(self):
        """Hunk content lines are correctly captured."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_SINGLE_FILE)

        hunk = patch_files[0].hunks[0]
        content_str = "\n".join(hunk.content)
        assert "+void new_func()" in content_str
        assert "+    // new function" in content_str

    def test_empty_patch(self):
        """Empty patch returns empty list."""
        assert parse_patch_into_hunks("") == []
        assert parse_patch_into_hunks("random text\nno diff here") == []


class TestPatchHunkHashKey:
    """Tests for PatchHunk.hash_key()."""

    def test_same_content_same_hash(self):
        """Identical hunks produce same hash."""
        hunk1 = PatchHunk(
            file_path="src/util.c",
            header="@@ -10,6 +10,10 @@",
            content=["+void new_func() {", "+}"],
        )
        hunk2 = PatchHunk(
            file_path="src/util.c",
            header="@@ -20,6 +20,10 @@",  # Different line numbers
            content=["+void new_func() {", "+}"],
        )
        # Same file + same content = same hash (line numbers ignored)
        assert hunk1.hash_key() == hunk2.hash_key()

    def test_different_file_different_hash(self):
        """Same content in different files produces different hash."""
        hunk1 = PatchHunk(
            file_path="src/util.c",
            header="@@ -10,6 +10,10 @@",
            content=["+void new_func() {", "+}"],
        )
        hunk2 = PatchHunk(
            file_path="src/other.c",
            header="@@ -10,6 +10,10 @@",
            content=["+void new_func() {", "+}"],
        )
        assert hunk1.hash_key() != hunk2.hash_key()

    def test_different_content_different_hash(self):
        """Different content produces different hash."""
        hunk1 = PatchHunk(
            file_path="src/util.c",
            header="@@ -10,6 +10,10 @@",
            content=["+void func_a() {", "+}"],
        )
        hunk2 = PatchHunk(
            file_path="src/util.c",
            header="@@ -10,6 +10,10 @@",
            content=["+void func_b() {", "+}"],
        )
        assert hunk1.hash_key() != hunk2.hash_key()


class TestDeduplicateHunks:
    """Tests for deduplicate_hunks()."""

    def test_no_duplicates(self):
        """All unique hunks are preserved."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_TWO_FILES)
        applied_hashes: set[str] = set()

        unique, new_hashes = deduplicate_hunks(patch_files, applied_hashes)

        assert len(unique) == 2
        assert len(new_hashes) == 2

    def test_removes_duplicate_hunks(self):
        """Duplicate hunks from second patch are removed."""
        patch1 = parse_patch_into_hunks(SAMPLE_PATCH_TWO_FILES)
        patch2 = parse_patch_into_hunks(SAMPLE_PATCH_OVERLAPPING)

        # Apply first patch
        applied_hashes: set[str] = set()
        unique1, applied_hashes = deduplicate_hunks(patch1, applied_hashes)
        assert len(unique1) == 2  # util.c and main.c

        # Apply second patch - util.c hunk should be deduplicated
        unique2, applied_hashes = deduplicate_hunks(patch2, applied_hashes)
        assert len(unique2) == 1  # Only other.c
        assert unique2[0].file_path == "src/other.c"

    def test_empty_result_when_all_duplicates(self):
        """Returns empty list when all hunks are duplicates."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_SINGLE_FILE)

        # Apply once
        applied_hashes: set[str] = set()
        _, applied_hashes = deduplicate_hunks(patch_files, applied_hashes)

        # Apply same patch again
        unique, _ = deduplicate_hunks(patch_files, applied_hashes)
        assert unique == []


class TestWritePatchFilesToDiff:
    """Tests for write_patch_files_to_diff()."""

    def test_roundtrip(self):
        """Parse and write back produces valid diff."""
        patch_files = parse_patch_into_hunks(SAMPLE_PATCH_TWO_FILES)
        output = write_patch_files_to_diff(patch_files)

        # Should contain key elements
        assert "diff --git" in output
        assert "--- a/src/util.c" in output
        assert "+++ b/src/util.c" in output
        assert "+void new_func()" in output

    def test_empty_list(self):
        """Empty list produces empty string with newline."""
        assert write_patch_files_to_diff([]) == "\n"


class TestActivemqRealPatches:
    """Integration tests using real activemq patches."""

    @pytest.fixture
    def activemq_patches(self) -> list[Path]:
        """Get activemq patch files if available."""
        base = Path("benchmarks/atlanta-activemq-delta-01/.aixcc/ActivemqOne")
        if not base.exists():
            pytest.skip("Activemq benchmark not available")

        patches = []
        for cpv_dir in sorted(base.glob("cpv_*")):
            patch_file = cpv_dir / "patches" / "patch_0.diff"
            if patch_file.exists():
                patches.append(patch_file)
        return patches

    def test_activemq_hunk_deduplication(self, activemq_patches: list[Path]):
        """Verify OpenWireUtil hunk is deduplicated across activemq patches."""
        if len(activemq_patches) < 2:
            pytest.skip("Need at least 2 patches")

        applied_hashes: set[str] = set()
        openwire_hunk_applied = False

        for i, patch_file in enumerate(activemq_patches[:3]):  # Test first 3
            content = patch_file.read_text()
            patch_files = parse_patch_into_hunks(content)

            # Check if this patch has OpenWireUtil hunk
            has_openwire = any("OpenWireUtil" in pf.file_path for pf in patch_files)

            unique, applied_hashes = deduplicate_hunks(patch_files, applied_hashes)

            # After first patch with OpenWireUtil, subsequent ones should skip it
            if i > 0 and has_openwire and openwire_hunk_applied:
                openwire_in_unique = any(
                    "OpenWireUtil" in pf.file_path for pf in unique
                )
                assert not openwire_in_unique, (
                    f"OpenWireUtil hunk should be deduplicated in patch {i}"
                )

            if has_openwire:
                openwire_hunk_applied = True
