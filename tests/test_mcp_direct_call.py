#!/usr/bin/env python3
"""
Test direct MCP tool calls to verify they work correctly.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import MCP server module
import crsbench.migration.crsbench_mcp_server as mcp_server


async def test_get_benchmark_info():
    """Test get_benchmark_info tool."""
    print("=" * 70)
    print("TEST 1: get_benchmark_info")
    print("=" * 70)

    benchmark_name = "libexif-delta-01"
    print(f"Calling: get_benchmark_info('{benchmark_name}')")

    result = await mcp_server.get_benchmark_info(benchmark_name)

    print("\nResult:")
    import json

    print(json.dumps(result, indent=2))

    if "error" in result:
        print("\n❌ FAILED: Error in result")
        return False

    print("\n✅ PASSED")
    return True


async def test_build_benchmark():
    """Test build_benchmark tool."""
    print("\n" + "=" * 70)
    print("TEST 2: build_benchmark")
    print("=" * 70)

    benchmark_name = "libexif-delta-01"
    print(f"Calling: build_benchmark('{benchmark_name}')")
    print("Note: This will take time as it builds the Docker image...")

    result = await mcp_server.build_benchmark(benchmark_name)

    print(f"\nResult: {result}")

    if result:
        print("\n✅ PASSED: Build succeeded")
        return True
    print("\n❌ FAILED: Build failed")
    return False


async def test_check_test_sh():
    """Test check_test_sh tool."""
    print("\n" + "=" * 70)
    print("TEST 3: check_test_sh")
    print("=" * 70)

    benchmark_name = "libexif-delta-01"
    print(f"Calling: check_test_sh('{benchmark_name}')")
    print("Note: This requires the Docker image to be built first...")

    result = await mcp_server.check_test_sh(benchmark_name)

    print("\nResult (last 500 chars):")
    if len(result) > 500:
        print("..." + result[-500:])
    else:
        print(result)

    if "Error:" in result:
        print("\n❌ FAILED: Error in result")
        return False

    if "test.sh execution succeeded" in result:
        print("\n✅ PASSED: test.sh succeeded")
        return True
    if "test.sh failed" in result:
        print("\n⚠️  WARNING: test.sh failed (expected if test.sh has issues)")
        return True  # Not a failure of the MCP tool itself

    print("\n✅ PASSED: Tool executed")
    return True


async def main():
    """Run all direct MCP tool tests."""
    print("🚀 Testing MCP Tools Directly\n")

    results = {}

    # Test 1: get_benchmark_info
    try:
        results["get_benchmark_info"] = await test_get_benchmark_info()
    except Exception as e:
        print(f"\n❌ Exception: {e}")
        import traceback

        traceback.print_exc()
        results["get_benchmark_info"] = False

    # Test 2: build_benchmark
    try:
        results["build_benchmark"] = await test_build_benchmark()
    except Exception as e:
        print(f"\n❌ Exception: {e}")
        import traceback

        traceback.print_exc()
        results["build_benchmark"] = False

    # Test 3: check_test_sh (only if build succeeded)
    if results.get("build_benchmark"):
        try:
            results["check_test_sh"] = await test_check_test_sh()
        except Exception as e:
            print(f"\n❌ Exception: {e}")
            import traceback

            traceback.print_exc()
            results["check_test_sh"] = False
    else:
        print("\n⚠️  Skipping check_test_sh (build failed)")
        results["check_test_sh"] = None

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for test_name, result in results.items():
        if result is None:
            status = "⏭️  SKIPPED"
        elif result:
            status = "✅ PASSED"
        else:
            status = "❌ FAILED"
        print(f"{status}: {test_name}")

    # Return exit code
    failed = [name for name, result in results.items() if result is False]
    if failed:
        print(f"\n❌ {len(failed)} test(s) failed")
        return 1
    print("\n✅ All tests passed")
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
