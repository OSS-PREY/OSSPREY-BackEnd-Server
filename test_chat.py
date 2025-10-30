#!/usr/bin/env python3
"""
Simple test script for the chat endpoint.
Run this to verify the chat API is working correctly.
"""

import requests
import json
import sys

# Try multiple base URLs
BASE_URLS = [
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "https://ossprey.ngrok.app"
]

# Headers to bypass ngrok warning page
HEADERS = {
    'ngrok-skip-browser-warning': 'true',
    'Content-Type': 'application/json'
}

def find_working_base_url():
    """Find which base URL is accessible."""
    for url in BASE_URLS:
        try:
            print(f"Trying {url}...")
            response = requests.get(f"{url}/api/health", headers=HEADERS, timeout=5)
            if response.status_code == 200:
                print(f"✓ Found working endpoint: {url}")
                return url
        except:
            continue
    return None

def test_health(base_url):
    """Test the health check endpoint."""
    print("\nTesting health endpoint...")
    try:
        response = requests.get(f"{base_url}/api/health", headers=HEADERS)
        if response.status_code == 200:
            print("✓ Health check passed:", response.json())
            return True
        else:
            print("✗ Health check failed:", response.status_code)
            print("  Response:", response.text)
            return False
    except requests.exceptions.ConnectionError:
        print(f"✗ Cannot connect to {base_url}. Make sure the server is running.")
        return False
    except Exception as e:
        print(f"✗ Health check error: {e}")
        return False

def test_chat(base_url, message, repo_name=""):
    """Test the chat endpoint with a message."""
    print(f"\nTesting chat endpoint with message: '{message}'")
    if repo_name:
        print(f"Repository context: {repo_name}")

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            headers=HEADERS,
            json={
                "message": message,
                "repoName": repo_name
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            print("✓ Chat response received:")
            print(f"  Response: {data.get('response', 'No response')[:100]}...")
            return True
        else:
            try:
                error_data = response.json()
                print(f"✗ Chat request failed ({response.status_code}):")
                print(f"  Error: {error_data.get('error', 'Unknown error')}")
            except:
                print(f"✗ Chat request failed ({response.status_code}):")
                print(f"  Response: {response.text}")
            return False
    except requests.exceptions.Timeout:
        print("✗ Request timed out. This might happen if Ollama is slow to respond.")
        return False
    except Exception as e:
        print(f"✗ Chat error: {e}")
        return False

def main():
    print("=" * 60)
    print("OSSPREY Chat API Test Suite")
    print("=" * 60)
    print("\nSearching for accessible backend endpoint...")

    # Find working base URL
    base_url = find_working_base_url()

    if not base_url:
        print("\n⚠ Cannot find accessible backend server.")
        print("\nTried the following URLs:")
        for url in BASE_URLS:
            print(f"  - {url}")
        print("\nPlease ensure:")
        print("  1. Backend server is running (flask run or python run.py)")
        print("  2. If using ngrok, update the URL in the BASE_URLS list")
        print("  3. Firewall/network allows connections")
        return

    print("\n" + "=" * 60)

    # Test health endpoint
    if not test_health(base_url):
        print("\n⚠ Health check failed.")
        return

    print("\n" + "-" * 60)

    # Test chat without repo context
    test_chat(base_url, "What is CI/CD?")

    print("\n" + "-" * 60)

    # Test chat with repo context
    test_chat(
        base_url,
        "How can I improve my project's sustainability?",
        "apache/airflow"
    )

    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
