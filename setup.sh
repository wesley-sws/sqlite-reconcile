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

has_command() {
    command -v "$1" >/dev/null 2>&1
}

add_sqlite_paths() {
    # Common Homebrew and local tool directories for sqlite/sqldiff.
    for p in \
        "/opt/homebrew/opt/sqlite/bin" \
        "/usr/local/opt/sqlite/bin" \
        "/opt/homebrew/bin" \
        "/usr/local/bin" \
        "$REPO_ROOT/tools/bin"
    do
        if [ -d "$p" ]; then
            export PATH="$p:$PATH"
        fi
    done
}

version_ge() {
    # Returns success if $1 >= $2 for dotted numeric versions.
    [ "$(printf '%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]
}

ensure_line_in_file() {
    local line="$1"
    local file="$2"

    touch "$file"
    if ! grep -Fqx "$line" "$file"; then
        echo "$line" >> "$file"
    fi
}

install_sqldiff_global_shim() {
    local sqldiff_path
    sqldiff_path=$(command -v sqldiff)

    local local_bin="$HOME/.local/bin"
    mkdir -p "$local_bin"

    ln -sf "$sqldiff_path" "$local_bin/sqldiff"

    local path_line='export PATH="$HOME/.local/bin:$PATH"'
    ensure_line_in_file "$path_line" "$HOME/.zshrc"
    ensure_line_in_file "$path_line" "$HOME/.bashrc"

    export PATH="$HOME/.local/bin:$PATH"
}

build_sqldiff_from_source() {
    if ! has_command curl; then
        echo "✗ curl is required to build sqldiff from source."
        return 1
    fi
    if ! has_command tar; then
        echo "✗ tar is required to build sqldiff from source."
        return 1
    fi
    if ! has_command make; then
        echo "✗ make is required to build sqldiff from source."
        return 1
    fi

    local build_root
    build_root=$(mktemp -d)
    local tarball="$build_root/sqlite-release.tar.gz"

    echo "Building sqldiff from SQLite source (release tarball)..."
    if ! curl -fsSL "https://www.sqlite.org/src/tarball/sqlite.tar.gz?r=release" -o "$tarball"; then
        echo "✗ Failed to download SQLite release source tarball."
        rm -rf "$build_root"
        return 1
    fi

    if ! tar -xzf "$tarball" -C "$build_root"; then
        echo "✗ Failed to extract SQLite source tarball."
        rm -rf "$build_root"
        return 1
    fi

    local src_dir
    src_dir=$(find "$build_root" -maxdepth 1 -type d -name "sqlite*" | head -n1)
    if [ -z "$src_dir" ]; then
        echo "✗ Could not locate extracted SQLite source directory."
        rm -rf "$build_root"
        return 1
    fi

    if ! (cd "$src_dir" && ./configure >/dev/null && make sqldiff >/dev/null); then
        echo "✗ Failed to build sqldiff from source."
        rm -rf "$build_root"
        return 1
    fi

    mkdir -p "$REPO_ROOT/tools/bin"
    cp "$src_dir/sqldiff" "$REPO_ROOT/tools/bin/sqldiff"
    chmod +x "$REPO_ROOT/tools/bin/sqldiff"
    rm -rf "$build_root"

    add_sqlite_paths
    return 0
}

install_sqlite_tools() {
    OS="$(uname -s)"

    if [ "$OS" = "Darwin" ]; then
        if ! has_command brew; then
            echo "✗ Homebrew not found. Install Homebrew from https://brew.sh and re-run setup."
            exit 1
        fi

        echo "Installing SQLite via Homebrew..."
        brew install sqlite

        add_sqlite_paths
        return
    fi

    case "$OS" in
        Linux)
            if has_command apt-get; then
                echo "Installing SQLite via apt-get..."
                sudo apt-get update
                sudo apt-get install -y sqlite3 sqlite3-tools
            elif has_command dnf; then
                echo "Installing SQLite via dnf..."
                sudo dnf install -y sqlite
            elif has_command yum; then
                echo "Installing SQLite via yum..."
                sudo yum install -y sqlite
            elif has_command pacman; then
                echo "Installing SQLite via pacman..."
                sudo pacman -Sy --noconfirm sqlite
            elif has_command zypper; then
                echo "Installing SQLite via zypper..."
                sudo zypper install -y sqlite3
            else
                echo "✗ Unsupported Linux package manager. Install sqlite3 and sqldiff manually."
                exit 1
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT)
            if has_command winget; then
                echo "Installing SQLite via winget..."
                winget install -e --id SQLite.SQLite
            elif has_command choco; then
                echo "Installing SQLite via Chocolatey..."
                choco install -y sqlite
            elif has_command scoop; then
                echo "Installing SQLite via Scoop..."
                scoop install sqlite
            else
                echo "✗ Could not find winget/choco/scoop. Install SQLite manually and ensure sqlite3 and sqldiff are on PATH."
                exit 1
            fi
            ;;
        *)
            echo "✗ Unsupported OS: $OS"
            echo "Install sqlite3 and sqldiff manually and re-run setup."
            exit 1
            ;;
    esac
}

add_sqlite_paths

if ! has_command sqlite3 || ! has_command sqldiff; then
    echo "sqlite3 and/or sqldiff not found. Attempting installation..."
    install_sqlite_tools
fi

if ! has_command sqldiff; then
    echo "sqldiff is still missing. Attempting local source build fallback..."
    build_sqldiff_from_source || true
fi

if ! has_command sqlite3; then
    echo "✗ sqlite3 not found after installation."
    exit 1
fi

if ! has_command sqldiff; then
    echo "✗ sqldiff not found after installation."
    echo "  This can happen on some package builds that ship sqlite3 but not sqldiff."
    echo "  Install sqldiff manually or keep using the locally built binary at $REPO_ROOT/tools/bin/sqldiff."
    exit 1
fi

SQLITE_VERSION=$(sqlite3 --version | awk '{print $1}')
MIN_SQLITE_VERSION="3.35.0"

if ! version_ge "$SQLITE_VERSION" "$MIN_SQLITE_VERSION"; then
    echo "✗ SQLite $MIN_SQLITE_VERSION+ required, but found $SQLITE_VERSION"
    echo "  Please upgrade sqlite3 and re-run setup."
    exit 1
fi

echo "✓ sqlite3 $SQLITE_VERSION found"
echo "✓ sqldiff found"

# Make sqldiff available as a universal user command without requiring
# users to manually locate tools/bin.
install_sqldiff_global_shim

if ! has_command sqldiff; then
    echo "✗ sqldiff could not be exposed globally."
    exit 1
fi

echo "✓ sqldiff is available globally (user-level)"

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
git config merge.sqlite.name "SQLite merge driver" # --global for driver to work across all repos
git config merge.sqlite.driver "env PATH=$REPO_ROOT/tools/bin:$PATH $REPO_ROOT/venv/bin/python $REPO_ROOT/src/sqlite-reconcile %O %A %B %L %P"

echo "✓ Setup complete!"
echo "  Virtual environment: $REPO_ROOT/venv"
echo "  Driver: $REPO_ROOT/src/sqlite-reconcile"
echo "  File patterns: *.db, *.sqlite"
echo "  Note: Run 'source ~/.zshrc' (or open a new terminal) for global 'sqldiff' in the current shell session."
