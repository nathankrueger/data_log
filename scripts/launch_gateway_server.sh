#!/bin/bash
# Launch the gateway server with the project's virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/.venv/bin/activate"
python "$PROJECT_DIR/gateway_server.py" "$@"
