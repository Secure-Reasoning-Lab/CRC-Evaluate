"""True E2E tests for POV verification with actual Docker builds.

These tests run the full verification pipeline including:
- Building variant projects with Docker
- Running POVs against built fuzzers
- Verifying correct CPV matching

Requirements:
- Docker must be running
- oss-fuzz directory must exist
- sanity-mock-c-delta-01 benchmark must be available

These tests are slow (~20-30s) due to Docker builds.
Mark with @pytest.mark.integration to skip in unit test runs.
"""

import subprocess
from pathlib import Path

import pytest


# Skip all tests if Docker is not available
def docker_available():
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


class TestPOVVerificationE2E:
    """True E2E tests using sanity-mock-c-delta-01 benchmark."""

    @pytest.fixture
    def benchmark_path(self) -> Path:
        path = Path("benchmarks/sanity-mock-c-delta-01")
        if not path.exists():
            pytest.skip(f"Benchmark not found: {path}")
        return path

    @pytest.fixture
    def oss_fuzz_path(self) -> Path:
        path = Path("oss-fuzz")
        if not path.exists():
            pytest.skip(f"oss-fuzz not found: {path}")
        return path

    def run_verify(
        self,
        benchmark_path: Path,
        harness: str,
        pov_dir: Path,
        oss_fuzz_path: Path,
    ) -> str:
        """Run crsbench verify and return stdout."""
        cmd = [
            "uv",
            "run",
            "crsbench",
            "verify",
            str(benchmark_path),
            "--harness",
            harness,
            "--pov-dir",
            str(pov_dir),
            "--oss-fuzz",
            str(oss_fuzz_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        return result.stdout

    def test_cpv0_pov_matches_cpv0(self, benchmark_path, oss_fuzz_path):
        """POV from cpv_0 should correctly match cpv_0."""
        pov_dir = (
            benchmark_path / ".aixcc" / "fuzz_process_input_header" / "cpv_0" / "blobs"
        )
        if not pov_dir.exists():
            pytest.skip(f"POV directory not found: {pov_dir}")

        output = self.run_verify(
            benchmark_path=benchmark_path,
            harness="fuzz_process_input_header",
            pov_dir=pov_dir,
            oss_fuzz_path=oss_fuzz_path,
        )

        # Check for expected keywords in output
        assert "cpv_matched" in output, f"Expected 'cpv_matched' in output:\n{output}"
        assert "cpv_0" in output, f"Expected 'cpv_0' in output:\n{output}"

    def test_cpv1_pov_matches_cpv1(self, benchmark_path, oss_fuzz_path):
        """POV from cpv_1 should correctly match cpv_1."""
        pov_dir = (
            benchmark_path / ".aixcc" / "fuzz_parse_buffer_section" / "cpv_1" / "blobs"
        )
        if not pov_dir.exists():
            pytest.skip(f"POV directory not found: {pov_dir}")

        output = self.run_verify(
            benchmark_path=benchmark_path,
            harness="fuzz_parse_buffer_section",
            pov_dir=pov_dir,
            oss_fuzz_path=oss_fuzz_path,
        )

        # Check for expected keywords in output
        assert "cpv_matched" in output, f"Expected 'cpv_matched' in output:\n{output}"
        assert "cpv_1" in output, f"Expected 'cpv_1' in output:\n{output}"

    def test_both_cpvs_verified(self, benchmark_path, oss_fuzz_path):
        """Verify all POVs from meta.yaml - should find both CPVs."""
        cmd = [
            "uv",
            "run",
            "crsbench",
            "verify",
            str(benchmark_path),
            "--oss-fuzz",
            str(oss_fuzz_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = result.stdout

        # Check summary shows both CPVs found
        assert "cpv_0" in output, f"Expected 'cpv_0' in output:\n{output}"
        assert "cpv_1" in output, f"Expected 'cpv_1' in output:\n{output}"
        assert "CPVs triggered" in output, f"Expected summary in output:\n{output}"


class TestBuildUIDOwnership:
    """Test that BUILD_UID correctly sets file ownership."""

    @pytest.fixture
    def oss_fuzz_path(self) -> Path:
        path = Path("oss-fuzz")
        if not path.exists():
            pytest.skip(f"oss-fuzz not found: {path}")
        return path

    def test_build_output_owned_by_current_user(self, oss_fuzz_path):
        """Verify build output is owned by current user (BUILD_UID works)."""
        import os

        build_out = oss_fuzz_path / "build" / "out"
        if not build_out.exists():
            pytest.skip("No build output exists - run verification first")

        current_uid = os.getuid()

        # Check at least one variant directory
        variant_dirs = list(build_out.glob("sanity-mock-c-delta-01-*"))
        if not variant_dirs:
            pytest.skip("No sanity-mock-c variants built")

        for variant_dir in variant_dirs:
            # Check fuzzer binaries (not the libfuzzer_default_out dirs which are from reproduce)
            for fuzzer in variant_dir.glob("fuzz_*"):
                if fuzzer.is_file():
                    stat = fuzzer.stat()
                    assert stat.st_uid == current_uid, (
                        f"{fuzzer} owned by {stat.st_uid}, expected {current_uid}"
                    )
