#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/.venv/bin/activate"

# Run pytest from the project root so it picks up pytest.ini and testpaths
cd "$SCRIPT_DIR"
pytest "$@"
