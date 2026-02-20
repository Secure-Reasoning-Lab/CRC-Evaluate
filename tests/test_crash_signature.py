"""Unit tests for crash signature parsing and hashing.

Tests for crsbench/evaluation/verification/crash_signature.py.
"""

from crsbench.evaluation.verification.crash_signature import (
    StackFrame,
    compute_signature_hash,
    parse_crash_signature,
)

# Sample ASAN crash logs for testing
ASAN_HEAP_BUFFER_OVERFLOW = """\
=================================================================
==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000210
READ of size 4 at 0x602000000210 thread T0
    #0 0x55f8a1 in parse_input /src/parser.c:42:15
    #1 0x55f9b2 in process_data /src/processor.c:88:3
    #2 0x55fab3 in main /src/main.c:12:5
    #3 0x7f1234 in __libc_start_main (libc.so.6+0x29d90)
    #4 0x55f000 in _start (binary+0x1000)

SUMMARY: AddressSanitizer: heap-buffer-overflow /src/parser.c:42:15 in parse_input
"""

ASAN_STACK_BUFFER_OVERFLOW = """\
=================================================================
==99999==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x7ffc12345678
WRITE of size 8 at 0x7ffc12345678 thread T0
    #0 0x55f8a1 in vulnerable_func /src/vuln.c:10:5
    #1 0x55f9b2 in caller_func /src/caller.c:20:3

SUMMARY: AddressSanitizer: stack-buffer-overflow /src/vuln.c:10:5 in vulnerable_func
"""

ASAN_WITH_SANITIZER_INTERNALS = """\
=================================================================
==11111==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000000010
READ of size 1 at 0x602000000010 thread T0
    #0 0x4a1234 in __interceptor_strlen (binary+0x1234)
    #1 0x4a5678 in __asan_memcpy (binary+0x5678)
    #2 0x55f8a1 in real_function /src/code.c:50:10
    #3 0x55f9b2 in another_function /src/other.c:75:3
    #4 0x55fab3 in main /src/main.c:100:5

SUMMARY: AddressSanitizer: heap-use-after-free /src/code.c:50:10 in real_function
"""

ASAN_NO_SOURCE_INFO = """\
=================================================================
==22222==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000
    #0 0x55f8a1 in some_func (binary+0x8a1)
    #1 0x55f9b2 in another (libfoo.so+0x9b2)

SUMMARY: AddressSanitizer: SEGV on unknown address
"""

LIBFUZZER_TIMEOUT = """\
==43210==WARNING: HWAddressSanitizer: ...
ALARM: working on the last Unit for 180 seconds
       and target is not responded
SUMMARY: libFuzzer: timeout
"""

LIBFUZZER_OOM = """\
==55555== Out of memory
SUMMARY: libFuzzer: out-of-memory
"""

JAVA_EXCEPTION_LOG = """\
== Java Exception: java.lang.ArrayIndexOutOfBoundsException
    at com.example.Parser.parse(Parser.java:42)
    at com.example.Main.run(Main.java:15)
    at com.example.Main.main(Main.java:8)
"""

ASAN_UPPERCASE_HEX = """\
=================================================================
==33333==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x60200000ABCD
READ of size 4 at 0x60200000ABCD thread T0
    #0 0x55F8A1 in parse_input /src/parser.c:42:15
    #1 0x55F9B2 in process_data /src/processor.c:88:3

SUMMARY: AddressSanitizer: heap-buffer-overflow /src/parser.c:42:15 in parse_input
"""

ASAN_ERROR_NO_FRAMES = """\
=================================================================
==44444==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000210
READ of size 4 at 0x602000000210 thread T0

SUMMARY: AddressSanitizer: heap-buffer-overflow
"""

UNPARSEABLE_LOG = """\
Some random output
No crash signature here
Just noise
"""


class TestParseAsanCrashSignature:
    """Tests for parsing ASAN crash signatures."""

    def test_heap_buffer_overflow(self) -> None:
        """Test parsing typical ASAN heap-buffer-overflow crash."""
        sig = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW)

        assert sig is not None
        assert sig.crash_type == "heap-buffer-overflow"
        assert len(sig.frames) > 0
        assert sig.frames[0].function == "parse_input"
        assert sig.frames[0].file == "/src/parser.c"
        assert sig.frames[0].line == 42
        assert len(sig.signature_hash) == 16
        assert "SUMMARY:" in sig.raw_summary

    def test_stack_buffer_overflow(self) -> None:
        """Test parsing ASAN stack-buffer-overflow with 2 frames."""
        sig = parse_crash_signature(ASAN_STACK_BUFFER_OVERFLOW)

        assert sig is not None
        assert sig.crash_type == "stack-buffer-overflow"
        assert len(sig.frames) == 2
        assert sig.frames[0].function == "vulnerable_func"
        assert sig.frames[1].function == "caller_func"

    def test_sanitizer_internal_frames_filtered(self) -> None:
        """Test that __interceptor and __asan frames are filtered out."""
        sig = parse_crash_signature(ASAN_WITH_SANITIZER_INTERNALS)

        assert sig is not None
        assert sig.crash_type == "heap-use-after-free"
        # Internal frames should be filtered; first real frame is real_function
        func_names = [f.function for f in sig.frames]
        assert "__interceptor_strlen" not in func_names
        assert "__asan_memcpy" not in func_names
        assert "real_function" in func_names

    def test_uppercase_hex_addresses(self) -> None:
        """Test parsing ASAN frames with uppercase hex addresses (A-F)."""
        sig = parse_crash_signature(ASAN_UPPERCASE_HEX)

        assert sig is not None
        assert sig.crash_type == "heap-buffer-overflow"
        assert len(sig.frames) == 2
        assert sig.frames[0].function == "parse_input"
        assert sig.frames[0].file == "/src/parser.c"
        assert sig.frames[0].line == 42

    def test_no_source_info_uses_module(self) -> None:
        """Test parsing ASAN frames without source info (module+offset format)."""
        sig = parse_crash_signature(ASAN_NO_SOURCE_INFO)

        assert sig is not None
        assert sig.crash_type == "SEGV"
        assert len(sig.frames) == 2
        # Without source info, file should be the module name
        assert sig.frames[0].file == "binary"
        assert sig.frames[0].line == 0
        assert sig.frames[1].file == "libfoo.so"


class TestZeroFramesSoundness:
    """Tests that ASAN/Java errors with zero parseable frames return None.

    This prevents false-positive dedup: two different bugs with the same
    crash type but unparseable frames would share a hash if we used
    crash_type alone.
    """

    def test_asan_error_no_frames_returns_none(self) -> None:
        """Test that ASAN error with no stack frames returns None."""
        sig = parse_crash_signature(ASAN_ERROR_NO_FRAMES)
        assert sig is None


class TestParseLibfuzzerCrashSignature:
    """Tests for parsing libFuzzer timeout and OOM."""

    def test_timeout(self) -> None:
        """Test parsing libFuzzer timeout (no frames, just crash_type)."""
        sig = parse_crash_signature(LIBFUZZER_TIMEOUT)

        assert sig is not None
        assert sig.crash_type == "timeout"
        assert len(sig.frames) == 0
        assert sig.signature_hash != ""

    def test_out_of_memory(self) -> None:
        """Test parsing libFuzzer out-of-memory."""
        sig = parse_crash_signature(LIBFUZZER_OOM)

        assert sig is not None
        assert sig.crash_type == "out-of-memory"
        assert len(sig.frames) == 0


class TestParseJavaCrashSignature:
    """Tests for parsing Java exception stack traces."""

    def test_java_exception(self) -> None:
        """Test parsing Java exception from Jazzer output."""
        sig = parse_crash_signature(JAVA_EXCEPTION_LOG)

        assert sig is not None
        assert sig.crash_type == "java.lang.ArrayIndexOutOfBoundsException"
        assert len(sig.frames) == 3
        assert sig.frames[0].function == "com.example.Parser.parse"
        assert sig.frames[0].file == "Parser.java"
        assert sig.frames[0].line == 42


class TestUnparseableLog:
    """Tests for unparseable logs."""

    def test_unparseable_returns_none(self) -> None:
        """Test that unparseable log returns None."""
        sig = parse_crash_signature(UNPARSEABLE_LOG)
        assert sig is None

    def test_empty_string_returns_none(self) -> None:
        """Test that empty string returns None."""
        sig = parse_crash_signature("")
        assert sig is None

    def test_none_like_empty_returns_none(self) -> None:
        """Test that whitespace-only log still returns None."""
        sig = parse_crash_signature("   \n\n  ")
        assert sig is None


class TestDeterministicHashing:
    """Tests for deterministic crash signature hashing."""

    def test_same_log_same_hash(self) -> None:
        """Test that same log produces same hash."""
        sig1 = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW)
        sig2 = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW)

        assert sig1 is not None
        assert sig2 is not None
        assert sig1.signature_hash == sig2.signature_hash

    def test_different_crash_different_hash(self) -> None:
        """Test that different crashes produce different hashes."""
        sig1 = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW)
        sig2 = parse_crash_signature(ASAN_STACK_BUFFER_OVERFLOW)

        assert sig1 is not None
        assert sig2 is not None
        assert sig1.signature_hash != sig2.signature_hash

    def test_compute_signature_hash_deterministic(self) -> None:
        """Test compute_signature_hash is deterministic."""
        frames = [
            StackFrame(frame_num=0, function="func_a", file="a.c", line=10),
            StackFrame(frame_num=1, function="func_b", file="b.c", line=20),
        ]
        hash1 = compute_signature_hash("heap-buffer-overflow", frames)
        hash2 = compute_signature_hash("heap-buffer-overflow", frames)

        assert hash1 == hash2
        assert len(hash1) == 16


class TestTopNParameter:
    """Tests for top_n parameter limiting frames."""

    def test_top_n_limits_frames(self) -> None:
        """Test that top_n=2 limits to 2 frames."""
        sig = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW, top_n=2)

        assert sig is not None
        assert len(sig.frames) <= 2

    def test_top_n_1(self) -> None:
        """Test that top_n=1 returns only 1 frame."""
        sig = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW, top_n=1)

        assert sig is not None
        assert len(sig.frames) == 1
        assert sig.frames[0].function == "parse_input"

    def test_different_top_n_different_hash(self) -> None:
        """Test that different top_n produces different hash."""
        sig1 = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW, top_n=1)
        sig2 = parse_crash_signature(ASAN_HEAP_BUFFER_OVERFLOW, top_n=5)

        assert sig1 is not None
        assert sig2 is not None
        # Different frames means different hash (if more frames exist)
        if len(sig2.frames) > 1:
            assert sig1.signature_hash != sig2.signature_hash
