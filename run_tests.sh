#!/bin/bash
set -e

source .venv/bin/activate

# Run pytest with any passed arguments
pytest "$@"
