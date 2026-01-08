"""Unit tests for verification utility functions.

Tests for compute_content_hash() from crsbench/evaluation/verification/utils.py.
"""

from pathlib import Path

from crsbench.evaluation.verification.utils import compute_content_hash


class TestComputeContentHash:
    """Tests for compute_content_hash function."""

    def test_returns_16_char_hex_string(self, tmp_path: Path) -> None:
        """Hash should return exactly 16 hex characters."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"test content")

        result = compute_content_hash(test_file)

        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        """Identical content should produce identical hashes."""
        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"
        content = b"identical content for both files"

        file1.write_bytes(content)
        file2.write_bytes(content)

        hash1 = compute_content_hash(file1)
        hash2 = compute_content_hash(file2)

        assert hash1 == hash2

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        """Different content should produce different hashes."""
        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"

        file1.write_bytes(b"content A")
        file2.write_bytes(b"content B")

        hash1 = compute_content_hash(file1)
        hash2 = compute_content_hash(file2)

        assert hash1 != hash2

    def test_empty_file_has_consistent_hash(self, tmp_path: Path) -> None:
        """Empty files should have a consistent hash."""
        file1 = tmp_path / "empty1.bin"
        file2 = tmp_path / "empty2.bin"

        file1.write_bytes(b"")
        file2.write_bytes(b"")

        hash1 = compute_content_hash(file1)
        hash2 = compute_content_hash(file2)

        assert hash1 == hash2
        # SHA256 of empty content
        assert len(hash1) == 16

    def test_nonexistent_file_returns_empty_hash(self, tmp_path: Path) -> None:
        """Non-existent file should return hash of empty content."""
        nonexistent = tmp_path / "does_not_exist.bin"

        result = compute_content_hash(nonexistent)

        # Should return first 12 chars of SHA256("")
        assert len(result) == 16
        # Verify it matches the empty file hash
        empty_file = tmp_path / "empty.bin"
        empty_file.write_bytes(b"")
        assert result == compute_content_hash(empty_file)

    def test_binary_content(self, tmp_path: Path) -> None:
        """Should correctly hash binary content including null bytes."""
        test_file = tmp_path / "binary.bin"
        binary_content = bytes(range(256))  # All byte values 0-255

        test_file.write_bytes(binary_content)

        result = compute_content_hash(test_file)

        assert len(result) == 16
        # Hash should be deterministic
        assert result == compute_content_hash(test_file)

    def test_large_file_chunked_reading(self, tmp_path: Path) -> None:
        """Should correctly hash large files that require chunked reading."""
        test_file = tmp_path / "large.bin"
        # Create a file larger than the 8192 byte chunk size
        large_content = b"x" * 50000

        test_file.write_bytes(large_content)

        result = compute_content_hash(test_file)

        assert len(result) == 16
        # Hash should be deterministic
        assert result == compute_content_hash(test_file)

    def test_hash_is_sha256_first_16(self, tmp_path: Path) -> None:
        """Verify the hash is actually SHA256 truncated to 16 chars."""
        import hashlib

        test_file = tmp_path / "test.bin"
        content = b"known content for verification"
        test_file.write_bytes(content)

        expected = hashlib.sha256(content).hexdigest()[:16]
        result = compute_content_hash(test_file)

        assert result == expected
