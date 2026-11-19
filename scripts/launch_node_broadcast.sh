#!/bin/bash
# Launch the node broadcaster with the project's virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Kill any existing instance (orphaned manual runs, stale processes)
# Lock files live in ~/.cache/data_log (see utils/process_lock.py); the old
# /tmp location is still checked so an instance started by an older version
# is found too.
kill_existing() {
    local lock_file
    for lock_file in "$HOME/.cache/data_log/${1}.lock" "/tmp/data_log_${1}.lock"; do
        kill_pid_from "$lock_file" "$1"
    done
}

kill_pid_from() {
    local lock_file="$1"
    if [ -f "$lock_file" ]; then
        local pid
        pid=$(cat "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "Killing existing $2 process (PID $pid)..."
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

kill_existing "node"

source "$PROJECT_DIR/.venv/bin/activate"
export PYTHONPATH="$PROJECT_DIR"
cd "$PROJECT_DIR" && exec python -m node.data_log "$@"
