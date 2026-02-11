#!/bin/bash
#
# Set radio parameters (SF, BW) for all discovered nodes and the gateway.
#
# Uses majority rule: If >50% of nodes succeed, gateway is updated.
# If <=50% succeed, gateway is NOT updated to preserve connectivity.
#
# This is a thin wrapper around set_radio_params.py.
#
# Usage: set_radio_params.sh --sf 9 --bw 1 [options]
#        set_radio_params.sh --sf 9 --dry-run
#
# See set_radio_params.py --help for full options.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/set_radio_params.py" "$@"
