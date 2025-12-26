"""Pytest configuration and fixtures."""

from pathlib import Path

from dotenv import load_dotenv

# Load .env file from project root if it exists
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    load_dotenv(env_file)
