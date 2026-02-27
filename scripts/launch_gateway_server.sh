#!/bin/bash
# Launch the gateway server with the project's virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Kill any existing instance (orphaned manual runs, stale processes)
kill_existing() {
    local lock_file="/tmp/data_log_${1}.lock"
    if [ -f "$lock_file" ]; then
        local pid
        pid=$(cat "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "Killing existing $1 process (PID $pid)..."
            kill "$pid"
            for i in $(seq 1 30); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "PID $pid did not exit, sending SIGKILL..."
                kill -9 "$pid"
                sleep 0.5
            fi
        fi
    fi
}

kill_existing "gateway"

source "$PROJECT_DIR/.venv/bin/activate"
export PYTHONPATH="$PROJECT_DIR"
exec python "$PROJECT_DIR/gateway/server.py" "$@"
