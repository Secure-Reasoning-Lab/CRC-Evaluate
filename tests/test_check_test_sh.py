#!/usr/bin/env python3
"""
Test check_test_sh function in isolation.
This test verifies that test.sh can be executed in a Docker container.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import MCP server module
import crsbench.migration.crsbench_mcp_server as mcp_server


def check_image_exists(benchmark_name: str) -> bool:
    """Check if Docker image already exists."""
    image_tag = f"gcr.io/oss-fuzz/aixcc/{benchmark_name}"
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image_tag],
            capture_output=True,
            text=True,
            timeout=10
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


async def test_check_test_sh(benchmark_name: str):
    """
    Test check_test_sh tool for a specific benchmark.

    Args:
        benchmark_name: Name of the benchmark to test (e.g., "libexif-delta-01")

    Returns:
        True if test succeeds, False otherwise
    """
    print("=" * 70)
    print(f"Testing check_test_sh for: {benchmark_name}")
    print("=" * 70)

    # First, check if benchmark exists and has test.sh
    print("\n1. Getting benchmark info...")
    info = await mcp_server.get_benchmark_info(benchmark_name)

    if "error" in info:
        print(f"❌ FAILED: {info['error']}")
        return False

    print(f"   Language: {info.get('language', 'unknown')}")
    print(f"   Has test.sh: {info.get('has_test_sh', False)}")
    print(f"   Project source: {info.get('project_source_dir', 'Not found')}")

    if not info.get('has_test_sh'):
        print("❌ FAILED: No test.sh found for this benchmark")
        return False

    # Check if Docker image already exists
    print("\n2. Checking Docker image...")
    image_exists = check_image_exists(benchmark_name)

    if image_exists:
        print("   ✓ Docker image already exists (skipping build)")
    else:
        print("   Docker image not found, building...")
        print("   (This may take several minutes...)")
        build_result = await mcp_server.build_benchmark(benchmark_name)

        if not build_result.get("success"):
            print("❌ FAILED: Docker build failed")
            print("   Hint: Check that project source exists at:")
            print(f"   {info.get('project_source_dir', 'unknown')}")
            return False

        print("   ✓ Docker image built successfully")

    # Run check_test_sh
    print("\n3. Running test.sh in Docker container...")
    result = await mcp_server.check_test_sh(benchmark_name)

    print("\n" + "=" * 70)
    print("TEST.SH OUTPUT")
    print("=" * 70)
    print(result["output"])
    print("=" * 70)

    # Display result summary
    print("\n" + "=" * 70)
    print("RESULT SUMMARY")
    print("=" * 70)
    print(f"   Return code: {result['returncode']}")
    print(f"   Success: {result['success']}")
    print(f"   Timed out: {result['timed_out']}")
    print("=" * 70)

    # Check result
    if result["returncode"] == -1 and not result["timed_out"]:
        print("\n❌ FAILED: Error occurred during test.sh execution")
        return False

    if result["success"]:
        print("\n✅ PASSED: test.sh executed successfully (exit code 0)")
        return True
    elif result["timed_out"]:
        print("\n⚠️  test.sh timed out (took more than 10 minutes)")
        return True  # Tool worked, but test.sh is slow
    else:
        print(f"\n⚠️  test.sh failed with exit code {result['returncode']}")
        print("   This is not an MCP tool failure - test.sh needs debugging")
        return True  # Tool worked, but test.sh has issues


async def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python test_check_test_sh.py <benchmark-name>")
        print("\nExample:")
        print("  python test_check_test_sh.py libexif-delta-01")
        print("  python test_check_test_sh.py atlanta-curl-delta-01")
        return 1

    benchmark_name = sys.argv[1]

    print(f"🚀 Testing check_test_sh for benchmark: {benchmark_name}\n")

    try:
        success = await test_check_test_sh(benchmark_name)

        if success:
            print("\n✅ Test completed successfully")
            return 0
        else:
            print("\n❌ Test failed")
            return 1

    except Exception as e:
        print(f"\n❌ Exception occurred: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
