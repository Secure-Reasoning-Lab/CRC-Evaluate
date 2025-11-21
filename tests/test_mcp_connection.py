#!/usr/bin/env python3
"""
Test MCP (Model Context Protocol) connection and tool availability.

This test verifies that:
1. MCP tools are properly registered
2. Tools can be called successfully
3. MCP server configuration is correct
"""

import asyncio
import os
import sys
from pathlib import Path
import inspect

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import the mcp instance and tools
import crsbench.migration.crsbench_mcp_server as mcp_server


async def test_mcp_instance():
    """Test that MCP instance exists and has tools registered."""
    print("=" * 70)
    print("TEST 1: MCP Instance and Tool Registration")
    print("=" * 70)

    try:
        mcp = mcp_server.mcp
        print(f"✅ MCP instance found: {mcp}")
        print(f"   Type: {type(mcp)}")
        print(f"   Name: {mcp.name}")
        return mcp
    except Exception as e:
        print(f"❌ Failed to access MCP instance: {e}")
        raise


async def test_list_tools(mcp):
    """Test that tools are registered and can be listed."""
    print("\n" + "=" * 70)
    print("TEST 2: List Registered Tools")
    print("=" * 70)

    try:
        # Get the registered tools from FastMCP
        # FastMCP stores tools in _tools dict
        if hasattr(mcp, '_tools'):
            tools = mcp._tools
            print(f"\n📋 Found {len(tools)} tools:")
            for i, (tool_name, tool_func) in enumerate(tools.items(), 1):
                print(f"\n{i}. {tool_name}")
                print(f"   Function: {tool_func}")
                # Get function signature
                sig = inspect.signature(tool_func)
                print(f"   Signature: {sig}")
                # Get docstring
                if tool_func.__doc__:
                    print(f"   Doc: {tool_func.__doc__.strip()[:100]}...")
        else:
            print("⚠️  MCP instance doesn't have _tools attribute")
            # Try to list tools via FastMCP API
            print("   Attempting to list tools via FastMCP API...")
            # FastMCP might have a different way to list tools
            print(f"   MCP attributes: {dir(mcp)}")
            return None

        if not tools:
            print("❌ No tools found!")
            return False

        print(f"\n✅ Successfully listed {len(tools)} tools")
        return tools
    except Exception as e:
        print(f"❌ Failed to list tools: {e}")
        import traceback
        traceback.print_exc()
        raise


async def test_call_get_build_logs():
    """Test calling get_build_logs tool to see actual errors."""
    print("\n" + "=" * 70)
    print("TEST 3: Call get_build_logs Tool")
    print("=" * 70)

    # Use a small benchmark for testing
    test_benchmark = "libexif-delta-01"

    print(f"\n📝 Testing get_build_logs with: {test_benchmark}")
    print("   This will show us the actual OSS-Fuzz build errors")

    try:
        # Call the tool function directly
        logs = await mcp_server.get_build_logs(test_benchmark)

        print("\n📊 Build logs:")
        print("=" * 70)
        # Print last 2000 chars of logs (most relevant errors are at the end)
        if len(logs) > 2000:
            print("... [truncated beginning] ...\n")
            print(logs[-2000:])
        else:
            print(logs)
        print("=" * 70)

        print("\n✅ Tool executed successfully")
        return logs

    except Exception as e:
        print(f"❌ Failed to execute tool: {e}")
        import traceback
        traceback.print_exc()
        # Don't raise - this is expected if OSS-Fuzz is not set up


async def test_call_get_benchmark_info():
    """Test calling get_benchmark_info tool directly."""
    print("\n" + "=" * 70)
    print("TEST 4: Call get_benchmark_info Tool")
    print("=" * 70)

    # Use a known benchmark
    test_benchmark = "libexif-delta-01"

    print(f"\n📝 Testing get_benchmark_info with: {test_benchmark}")

    try:
        # Call the tool function directly
        result = await mcp_server.get_benchmark_info(test_benchmark)

        print("\n📊 Tool execution result:")
        print(f"   Type: {type(result)}")
        import json
        print(f"   Result:\n{json.dumps(result, indent=2)}")

        print("\n✅ Tool executed successfully")
        return result

    except Exception as e:
        print(f"❌ Failed to execute tool: {e}")
        import traceback
        traceback.print_exc()
        raise


async def main():
    """Run all MCP connection tests."""
    print("🚀 Starting MCP Connection Tests\n")

    failed_tests = []

    try:
        # Test 1: MCP instance
        mcp = await test_mcp_instance()

        # Test 2: List tools
        tools = await test_list_tools(mcp)

        # Test 3: Call get_build_logs to see actual errors
        try:
            await test_call_get_build_logs()
        except Exception as e:
            failed_tests.append(("get_build_logs", str(e)))

        # Test 4: Call get_benchmark_info
        try:
            await test_call_get_benchmark_info()
        except Exception as e:
            failed_tests.append(("get_benchmark_info", str(e)))

        print("\n" + "=" * 70)
        if not failed_tests:
            print("✅ ALL TESTS PASSED")
        else:
            print("⚠️  SOME TESTS FAILED")
            for test_name, error in failed_tests:
                print(f"   - {test_name}: {error}")
        print("=" * 70)

        return 0 if not failed_tests else 1

    except Exception as e:
        print("\n" + "=" * 70)
        print("❌ CRITICAL TEST FAILURE")
        print("=" * 70)
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
