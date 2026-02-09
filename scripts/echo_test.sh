#!/bin/bash
#
# Echo command reliability test.
#
# Runs the echo command in a loop at a configurable rate and collects
# success/failure statistics.
#
# Requires: 'jq' (sudo apt install jq)
#
# Usage: echo_test.sh -n <node_id> [-i <interval>] [-c <count>] [-g <gateway>] [-p <port>]
#
# Options:
#   -n <node_id>   Target node ID (required)
#   -i <interval>  Seconds between echo attempts (default: 1)
#   -c <count>     Number of iterations (default: infinite, Ctrl+C to stop)
#   -g <gateway>   Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>      Gateway port (default: $GATEWAY_PORT or 5001)
#   -h             Show this help
#
# Each echo sends a unique millisecond timestamp and validates the response.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    head -n 18 "$0" | tail -n 17 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

NODE_ID=""
INTERVAL=1
COUNT=0  # 0 = infinite
OPT_HOST=""
OPT_PORT=""

while getopts "n:i:c:g:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        i)
            INTERVAL="$OPTARG"
            ;;
        c)
            COUNT="$OPTARG"
            ;;
        g)
            OPT_HOST="$OPTARG"
            ;;
        p)
            OPT_PORT="$OPTARG"
            ;;
        h)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

# Node ID is required
if [ -z "$NODE_ID" ]; then
    echo "Error: -n <node_id> is required"
    usage
fi

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

# Statistics
TOTAL=0
SUCCESS=0
FAIL=0
MISMATCH=0

# Cleanup handler
cleanup() {
    echo ""
    echo "========================================"
    echo "Echo Test Results"
    echo "========================================"
    echo "Total attempts: $TOTAL"
    echo "Successful:     $SUCCESS"
    echo "Mismatched:     $MISMATCH"
    echo "Failed:         $FAIL"
    if [ $TOTAL -gt 0 ]; then
        RATE=$(echo "scale=1; $SUCCESS * 100 / $TOTAL" | bc)
        echo "Success rate:   ${RATE}%"
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

echo "Echo test: node=$NODE_ID, interval=${INTERVAL}s"
echo "Press Ctrl+C to stop and see results"
echo "----------------------------------------"

ITERATION=0
while true; do
    ITERATION=$((ITERATION + 1))
    TOTAL=$((TOTAL + 1))

    # Generate unique data: millisecond timestamp
    SEND_DATA=$(date '+%s%3N')

    # Run echo command - node_cmd.sh -w prints JSON body on success (exit 0),
    # or prints error to stderr and exits non-zero on failure
    RESPONSE=$("$SCRIPT_DIR/node_cmd.sh" -n "$NODE_ID" -c echo -a "$SEND_DATA" -w -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
    CMD_EXIT=$?

    TIMESTAMP=$(date '+%H:%M:%S')

    if [ $CMD_EXIT -eq 0 ]; then
        # Extract echoed data from response JSON
        ECHOED_DATA=$(echo "$RESPONSE" | jq -r '.data // .echo // .payload // .' 2>/dev/null)

        if [ "$ECHOED_DATA" = "$SEND_DATA" ]; then
            SUCCESS=$((SUCCESS + 1))
            echo "[$TIMESTAMP] #$ITERATION: OK (sent=$SEND_DATA)"
        else
            MISMATCH=$((MISMATCH + 1))
            echo "[$TIMESTAMP] #$ITERATION: MISMATCH (sent=$SEND_DATA, got=$ECHOED_DATA)"
        fi
    else
        FAIL=$((FAIL + 1))
        echo "[$TIMESTAMP] #$ITERATION: FAIL - $RESPONSE"
    fi

    # Check if we've reached the count limit
    if [ $COUNT -gt 0 ] && [ $ITERATION -ge $COUNT ]; then
        cleanup
    fi

    sleep "$INTERVAL"
done
