#!/bin/bash

# Installation script for CRSBench and oss-crs packages

set -e  # Exit on error

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing oss-crs package..."
pip install -e "${SCRIPT_DIR}/oss-crs"

echo "Installing crsbench package..."
pip install -e "${SCRIPT_DIR}"

echo ""
echo "Installation complete!"
echo "Available commands:"
echo "  - crsbench          : Main CRSBench evaluation tool"
echo "  - oss-crs           : CRS lifecycle tool (prepare/build-target/artifacts/run)"
