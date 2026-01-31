#!/bin/bash

usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Install or activate the data_log Python environment.

OPTIONS:
    -r, --reinstall    Remove existing .venv and reinstall from scratch
    -u, --update       Update dependencies from requirements.txt
    -h, --help         Display this help message

EXAMPLES:
    $0                 # Activate existing .venv or create if missing
    $0 --reinstall     # Force fresh installation
    $0 --update        # Install any new requirements
EOF
    exit 0
}

# Parse command line arguments
REINSTALL=false
UPDATE=false

while [[ $# -gt 0 ]]; do
    case $1 in
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
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

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
    else
        echo "Error: No .venv directory found. Run without --update first to create it."
        exit 1
    fi
    exit 0
fi

# Install or activate
if [ -d ".venv" ]; then
    echo "Activating existing virtual environment..."
    source .venv/bin/activate
else
    echo "Installing GPIO library..."
    sudo apt install python3-rpi-lgpio

    echo "Creating new virtual environment..."
    # use system packages for python3-rpi-lgpio access
    python3 -m venv --system-site-packages .venv
    source .venv/bin/activate
    echo "Updating pip..."
    pip install --upgrade pip
    echo "Installing requirements..."
    pip install -r requirements.txt
fi