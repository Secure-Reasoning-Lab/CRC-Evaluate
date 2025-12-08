#!/usr/bin/env python3
"""Test LiteLLM instance to verify it's working correctly.

This script tests a running LiteLLM instance with both real and mock models.

Usage:
    python scripts/test_litellm.py --port 4000
    python scripts/test_litellm.py --port 4000 --mock-only  # No API calls
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)


def test_health(base_url):
    """Test health endpoint."""
    print("=" * 60)
    print("Testing Health Endpoint")
    print("=" * 60)

    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        if response.status_code == 200:
            print("✓ Health check passed")
            print(f"  Response: {response.json()}")
            return True
        else:
            print(f"✗ Health check failed: {response.status_code}")
            print(f"  Response: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Health check error: {e}")
        return False


def test_models_list(base_url, api_key):
    """Test models listing endpoint."""
    print("\n" + "=" * 60)
    print("Testing Models List")
    print("=" * 60)

    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.get(f"{base_url}/models", headers=headers, timeout=5)

        if response.status_code == 200:
            data = response.json()
            models = data.get("data", [])
            print(f"✓ Found {len(models)} available models:")
            for model in models[:10]:  # Show first 10
                print(f"  - {model.get('id', 'unknown')}")
            if len(models) > 10:
                print(f"  ... and {len(models) - 10} more")
            return True
        else:
            print(f"✗ Models list failed: {response.status_code}")
            print(f"  Response: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Models list error: {e}")
        return False


def test_mock_completion(base_url, api_key):
    """Test completion with mock response (no real API call)."""
    print("\n" + "=" * 60)
    print("Testing Mock Completion (no API call)")
    print("=" * 60)

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "user", "content": "Say 'test successful'"}
        ],
        "mock_response": "test successful"  # Mock response, no API call
    }

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            print(f"✓ Mock completion successful")
            print(f"  Model: {data.get('model', 'unknown')}")
            print(f"  Response: {content}")
            print(f"  Usage: {data.get('usage', {})}")
            return True
        else:
            print(f"✗ Mock completion failed: {response.status_code}")
            print(f"  Response: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Mock completion error: {e}")
        return False


def test_real_completion(base_url, api_key, model="gpt-4o-mini"):
    """Test real completion (makes actual API call)."""
    print("\n" + "=" * 60)
    print(f"Testing Real Completion with {model}")
    print("=" * 60)
    print("WARNING: This will make a real API call and may incur costs!")

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Say 'Hello from LiteLLM!' in exactly those words."}
        ],
        "max_tokens": 20
    }

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            print(f"✓ Real completion successful")
            print(f"  Model: {data.get('model', 'unknown')}")
            print(f"  Response: {content}")
            print(f"  Tokens - Prompt: {usage.get('prompt_tokens', 0)}, "
                  f"Completion: {usage.get('completion_tokens', 0)}, "
                  f"Total: {usage.get('total_tokens', 0)}")

            # Check if response matches expected
            if "Hello from LiteLLM!" in content:
                print(f"  ✓ Response matches expected output")

            return True
        else:
            print(f"✗ Real completion failed: {response.status_code}")
            error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"error": response.text}
            print(f"  Error: {json.dumps(error_data, indent=2)}")

            # Check for common errors
            if response.status_code == 401:
                print("  Hint: Check LITELLM_MASTER_KEY is correct")
            elif response.status_code == 404:
                print(f"  Hint: Model '{model}' not found. Check default-models.yaml")
            elif response.status_code == 500:
                error_msg = error_data.get("error", {}).get("message", "")
                if "api_key" in error_msg.lower():
                    print(f"  Hint: Provider API key missing or invalid. Check .env file")

            return False
    except Exception as e:
        print(f"✗ Real completion error: {e}")
        return False


def test_streaming(base_url, api_key):
    """Test streaming completion."""
    print("\n" + "=" * 60)
    print("Testing Streaming Completion")
    print("=" * 60)

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "user", "content": "Count from 1 to 5"}
        ],
        "stream": True,
        "mock_response": "1, 2, 3, 4, 5"  # Mock for testing
    }

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=30
        )

        if response.status_code == 200:
            print(f"✓ Streaming response:")
            chunks = []
            for line in response.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    if line_str.startswith('data: '):
                        data_str = line_str[6:]  # Remove 'data: ' prefix
                        if data_str.strip() == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            content = chunk['choices'][0]['delta'].get('content', '')
                            if content:
                                chunks.append(content)
                                print(f"  {content}", end='', flush=True)
                        except json.JSONDecodeError:
                            pass
            print()  # Newline after streaming
            print(f"  ✓ Received {len(chunks)} chunks")
            return True
        else:
            print(f"✗ Streaming failed: {response.status_code}")
            print(f"  Response: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"✗ Streaming error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Test LiteLLM instance"
    )
    parser.add_argument(
        "--port", type=int, default=4000,
        help="LiteLLM port (default: 4000)"
    )
    parser.add_argument(
        "--host", default="localhost",
        help="LiteLLM host (default: localhost)"
    )
    parser.add_argument(
        "--api-key", default=None,
        help="LiteLLM API key (default: from LITELLM_MASTER_KEY env)"
    )
    parser.add_argument(
        "--mock-only", action="store_true",
        help="Only run mock tests (no real API calls)"
    )
    parser.add_argument(
        "--model", default="gpt-4o-mini",
        help="Model to test with (default: gpt-4o-mini)"
    )

    args = parser.parse_args()

    # Get API key
    api_key = args.api_key
    if not api_key:
        import os
        api_key = os.getenv("LITELLM_MASTER_KEY", "sk-test")

    base_url = f"http://{args.host}:{args.port}"

    print(f"Testing LiteLLM at {base_url}")
    print(f"API Key: {api_key[:10]}..." if len(api_key) > 10 else api_key)
    print()

    # Run tests
    results = {}

    results["health"] = test_health(base_url)
    results["models_list"] = test_models_list(base_url, api_key)
    results["mock_completion"] = test_mock_completion(base_url, api_key)
    results["streaming"] = test_streaming(base_url, api_key)

    if not args.mock_only:
        results["real_completion"] = test_real_completion(base_url, api_key, args.model)

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {test_name}")

    print(f"\nResults: {passed}/{total} tests passed")

    if passed == total:
        print("\n✓ All tests passed! LiteLLM is working correctly.")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
