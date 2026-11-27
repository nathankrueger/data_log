#!/bin/bash

usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Install or activate the data_log Python environment.

OPTIONS:
    -r, --reinstall      Remove existing .venv and reinstall from scratch
    -u, --update         Update dependencies from requirements.txt
    -p, --python PATH    Python interpreter to create .venv with (default: python3).
                         A path or a command name; Python 3.9 or newer. Only used
                         when .venv is created (first run, or with --reinstall).
    -h, --help           Display this help message

EXAMPLES:
    $0                                       # Activate existing .venv or create if missing
    $0 --reinstall                           # Force fresh installation
    $0 --update                              # Install any new requirements
    $0 --reinstall --python python3.12       # Rebuild .venv on another interpreter
    $0 --reinstall -p /opt/py/bin/python3    # ...or give its full path
EOF
    return 0
}

# Platform detection
IS_PI=false
if [ "$(uname -s)" = "Linux" ]; then
    IS_PI=true
fi

# Parse command line arguments
REINSTALL=false
UPDATE=false
PYTHON_BIN=python3
PYTHON_CUSTOM=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--python)
            if [ -z "$2" ]; then
                echo "Error: $1 needs an interpreter path or command name"
                return 1 2>/dev/null || exit 1
            fi
            PYTHON_BIN="$2"
            PYTHON_CUSTOM=true
            shift 2
            ;;
        --python=*)
            PYTHON_BIN="${1#*=}"
            PYTHON_CUSTOM=true
            shift
            ;;
        -r|--reinstall)
            REINSTALL=true
            shift
            ;;
        -u|--update)
            UPDATE=true
            shift
            ;;
        -h|--help)
            usage
            return 0 2>/dev/null || exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            return 1 2>/dev/null || exit 1
            ;;
    esac
done

# Resolve and check the interpreter before touching anything (only needed
# when a .venv will be built, or to compare against an explicit --python)
if [ "$REINSTALL" = true ] || [ ! -d ".venv" ] || [ "$PYTHON_CUSTOM" = true ]; then
    # Never look the interpreter up in the .venv being replaced (it may be active)
    SEARCH_PATH=":$PATH:"
    SEARCH_PATH="${SEARCH_PATH//:$(pwd)\/.venv\/bin:/:}"
    if [ -n "$VIRTUAL_ENV" ] && [ "$VIRTUAL_ENV" -ef .venv ]; then
        SEARCH_PATH="${SEARCH_PATH//:$VIRTUAL_ENV\/bin:/:}"
    fi
    SEARCH_PATH="${SEARCH_PATH#:}"
    SEARCH_PATH="${SEARCH_PATH%:}"
    PYTHON_PATH="$(PATH="$SEARCH_PATH"; command -v "$PYTHON_BIN" 2>/dev/null)"
    if [ -z "$PYTHON_PATH" ] || [ ! -x "$PYTHON_PATH" ]; then
        echo "Error: Python interpreter not found: $PYTHON_BIN"
        return 1 2>/dev/null || exit 1
    fi
    if ! "$PYTHON_PATH" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        echo "Error: $PYTHON_PATH is not Python 3.9+ with the venv module"
        echo "       (on Debian/Raspberry Pi OS: sudo apt install python3-venv)"
        return 1 2>/dev/null || exit 1
    fi
    PYTHON_VERSION="$("$PYTHON_PATH" -c 'import platform; print(platform.python_version())')"
fi

# An existing .venv keeps the interpreter it was built with
if [ "$PYTHON_CUSTOM" = true ] && [ "$REINSTALL" = false ] && [ -x ".venv/bin/python" ]; then
    VENV_VERSION="$(.venv/bin/python -c 'import platform; print(platform.python_version())' 2>/dev/null)"
    if [ "$VENV_VERSION" != "$PYTHON_VERSION" ]; then
        echo "Warning: .venv uses Python ${VENV_VERSION:-unknown}, not $PYTHON_VERSION ($PYTHON_PATH)."
        echo "         Add --reinstall to rebuild it with $PYTHON_BIN."
    fi
fi

# Handle reinstall option
if [ "$REINSTALL" = true ]; then
    echo "Reinstalling from scratch..."

    # Deactivate if currently in a virtual environment
    if [ -n "$VIRTUAL_ENV" ]; then
        echo "Deactivating current virtual environment..."
        deactivate 2>/dev/null || true
    fi

    # Remove existing .venv
    if [ -d ".venv" ]; then
        echo "Removing existing .venv..."
        rm -rf .venv
    fi
fi

# Handle update option
if [ "$UPDATE" = true ]; then
    if [ -d ".venv" ]; then
        echo "Activating virtual environment..."
        source .venv/bin/activate
        echo "Updating requirements..."
        pip install -r requirements.txt
        if [ "$IS_PI" = true ]; then
            pip install -r requirements-hw.txt
        fi
    else
        echo "Error: No .venv directory found. Run without --update first to create it."
        return 1 2>/dev/null || exit 1
    fi
    return 0 2>/dev/null || exit 0
fi

# Install or activate
if [ -d ".venv" ]; then
    echo "Activating existing virtual environment..."
    source .venv/bin/activate
else
    if [ "$IS_PI" = true ]; then
        echo "Installing GPIO library..."
        sudo apt install python3-rpi-lgpio

        if [ "$PYTHON_CUSTOM" = true ] && [ "$("$PYTHON_PATH" -c 'import sys; print(sys.base_prefix)')" != "/usr" ]; then
            echo "Note: python3-rpi-lgpio from apt is installed for the system Python;"
            echo "      $PYTHON_PATH may not see it (GPIO would fail to import)."
        fi
        echo "Creating new virtual environment with Python $PYTHON_VERSION ($PYTHON_PATH)..."
        # use system packages for python3-rpi-lgpio access
        "$PYTHON_PATH" -m venv --system-site-packages .venv || { return 1 2>/dev/null || exit 1; }
    else
        echo "Creating new virtual environment with Python $PYTHON_VERSION ($PYTHON_PATH) (dev mode — no hardware deps)..."
        "$PYTHON_PATH" -m venv .venv || { return 1 2>/dev/null || exit 1; }
    fi
    source .venv/bin/activate
    echo "Updating pip..."
    pip install --upgrade pip
    echo "Installing requirements..."
    pip install -r requirements.txt
    if [ "$IS_PI" = true ]; then
        echo "Installing hardware dependencies..."
        pip install -r requirements-hw.txt
    fi
fi
