#!/bin/bash
#
# Blink command reliability test.
#
# Sends blink commands in a loop at a configurable rate and collects
# success/failure statistics.
#
# Requires: 'jq' (sudo apt install jq)
#
# Usage: blink_test.sh [-n <node_id> | -B] [-i <interval>] [-c <count>] [-C <color>] [-d <duration>] [-b <brightness>] [-g <gateway>] [-p <port>]
#
# Options:
#   -n <node_id>     Target node ID
#   -B               Broadcast to all nodes
#   -i <interval>    Seconds between blink commands (default: 2)
#   -c <count>       Number of iterations (default: infinite, Ctrl+C to stop)
#   -C <color>       LED color: red, green, blue, yellow, cyan, magenta, white (default: green)
#   -d <duration>    Blink duration in seconds (default: 0.5)
#   -b <brightness>  LED brightness 0.0-1.0 (default: 1.0)
#   -g <gateway>     Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>        Gateway port (default: $GATEWAY_PORT or 5001)
#   -h               Show this help
#
# -n and -B are mutually exclusive. One must be specified.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    head -n 23 "$0" | tail -n 22 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

NODE_ID=""
BROADCAST=false
INTERVAL=2
COUNT=0  # 0 = infinite
COLOR="green"
DURATION="0.5"
BRIGHTNESS="1.0"
OPT_HOST=""
OPT_PORT=""

while getopts "n:Bi:c:C:d:b:g:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        B)
            BROADCAST=true
            ;;
        i)
            INTERVAL="$OPTARG"
            ;;
        c)
            COUNT="$OPTARG"
            ;;
        C)
            COLOR="$OPTARG"
            ;;
        d)
            DURATION="$OPTARG"
            ;;
        b)
            BRIGHTNESS="$OPTARG"
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

# Check for mutually exclusive options
if [ -n "$NODE_ID" ] && [ "$BROADCAST" = true ]; then
    echo "Error: -n and -B are mutually exclusive"
    usage
fi

# Require at least one targeting option
if [ -z "$NODE_ID" ] && [ "$BROADCAST" = false ]; then
    echo "Error: Must specify either -n <node_id> or -B"
    usage
fi

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

# Statistics
TOTAL=0
SUCCESS=0
FAIL=0

# Cleanup handler
cleanup() {
    echo ""
    echo "========================================"
    echo "Blink Test Results"
    echo "========================================"
    echo "Total attempts: $TOTAL"
    echo "Successful:     $SUCCESS"
    echo "Failed:         $FAIL"
    if [ $TOTAL -gt 0 ]; then
        RATE=$(echo "scale=1; $SUCCESS * 100 / $TOTAL" | bc)
        echo "Success rate:   ${RATE}%"
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

if [ "$BROADCAST" = true ]; then
    TARGET="broadcast"
else
    TARGET="node=$NODE_ID"
fi

echo "Blink test: $TARGET, interval=${INTERVAL}s"
echo "Blink params: color=$COLOR, duration=${DURATION}s, brightness=$BRIGHTNESS"
echo "Press Ctrl+C to stop and see results"
echo "----------------------------------------"

ITERATION=0
while true; do
    ITERATION=$((ITERATION + 1))
    TOTAL=$((TOTAL + 1))

    # Build blink args: color,duration,brightness
    BLINK_ARGS="$COLOR,$DURATION,$BRIGHTNESS"

    # Run blink command (fire-and-forget, no -w)
    if [ "$BROADCAST" = true ]; then
        OUTPUT=$("$SCRIPT_DIR/node_cmd.sh" -c blink -a "$BLINK_ARGS" -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
    else
        OUTPUT=$("$SCRIPT_DIR/node_cmd.sh" -n "$NODE_ID" -c blink -a "$BLINK_ARGS" -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
    fi

    TIMESTAMP=$(date '+%H:%M:%S')

    # Check if command was queued successfully
    # Requires non-empty response with status="queued"
    if [ -n "$OUTPUT" ] && echo "$OUTPUT" | jq -e '.status == "queued"' >/dev/null 2>&1; then
        SUCCESS=$((SUCCESS + 1))
        echo "[$TIMESTAMP] #$ITERATION: QUEUED ($COLOR, ${DURATION}s)"
    else
        FAIL=$((FAIL + 1))
        echo "[$TIMESTAMP] #$ITERATION: FAIL - ${OUTPUT:-<empty response>}"
    fi

    # Check if we've reached the count limit
    if [ $COUNT -gt 0 ] && [ $ITERATION -ge $COUNT ]; then
        cleanup
    fi

    sleep "$INTERVAL"
done
