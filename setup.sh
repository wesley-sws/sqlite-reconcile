#!/bin/bash

set -e  # Exit on error

# Setup script for sqlite-reconcile merge driver

REPO_ROOT=$(git rev-parse --show-toplevel)

echo "Setting up sqlite-reconcile..."

# Check Python 3.10+ is installed
if ! command -v python3 &> /dev/null; then
    echo "✗ Python 3 not found. Please install Python 3.10+"
    exit 1
fi

if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo "✗ Python 3.10+ required, but found $PYTHON_VERSION"
    echo "Install Python 3.10+ or set your PATH to use it"
    exit 1
fi

echo "✓ Python $(python3 --version | awk '{print $2}') found"

# Create virtual environment if it doesn't exist
if [ ! -d "$REPO_ROOT/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$REPO_ROOT/venv"
fi

# Activate virtual environment
source "$REPO_ROOT/venv/bin/activate"

# Install dependencies
echo "Installing dependencies..."
pip install -q -r "$REPO_ROOT/requirements.txt"

# Make the merge driver executable
chmod +x "$REPO_ROOT/src/sqlite-reconcile"

# Configure git merge driver for this repository (for testing/development)
git config merge.sqlite.name "SQLite merge driver"
git config merge.sqlite.driver "$REPO_ROOT/venv/bin/python $REPO_ROOT/src/sqlite-reconcile %O %A %B %L %P"

echo "✓ Setup complete!"
echo "  Virtual environment: $REPO_ROOT/venv"
echo "  Driver: $REPO_ROOT/src/sqlite-reconcile"
echo "  File patterns: *.db, *.sqlite"
