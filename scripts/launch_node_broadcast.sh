#!/bin/bash
# Launch the node broadcaster with the project's virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/.venv/bin/activate"
exec python "$PROJECT_DIR/node_broadcast.py" "$@"
