#!/bin/bash
#
# Discovery reliability test.
#
# Runs node discovery in a loop and validates that the same set of
# nodes is found each time. Optionally checks against an expected count.
#
# Requires: 'jq' (sudo apt install jq)
#
# Usage: discover_test.sh [-c <count>] [-a <retries>] [-i <interval>] [-r <rounds>]
#                         [-g <gateway>] [-p <port>]
#
# Options:
#   -c <count>    Expected number of nodes (validated each round)
#   -a <retries>  Discovery retries passed to gateway (default: gateway config)
#   -i <interval> Seconds between discover rounds (default: 5)
#   -r <rounds>   Number of rounds (default: infinite, Ctrl+C to stop)
#   -g <gateway>  Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>     Gateway port (default: $GATEWAY_PORT or 5001)
#   -h            Show this help

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    head -n 21 "$0" | tail -n 20 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

EXPECTED_COUNT=0  # 0 = don't check count
RETRIES=""
INTERVAL=5
ROUNDS=0  # 0 = infinite
OPT_HOST=""
OPT_PORT=""

while getopts "c:a:i:r:g:p:h" opt; do
    case $opt in
        c)
            EXPECTED_COUNT="$OPTARG"
            ;;
        a)
            RETRIES="$OPTARG"
            ;;
        i)
            INTERVAL="$OPTARG"
            ;;
        r)
            ROUNDS="$OPTARG"
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

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

# Statistics
TOTAL=0
SUCCESS=0
FAIL=0
MISMATCH=0

# Baseline (set after first successful discovery)
BASELINE_NODES=""
BASELINE_COUNT=0
BASELINE_SET=false

# Cleanup handler
cleanup() {
    echo ""
    echo "========================================"
    echo "Discovery Test Results"
    echo "========================================"
    echo "Total rounds:   $TOTAL"
    echo "Successful:     $SUCCESS"
    echo "Mismatched:     $MISMATCH"
    echo "Failed:         $FAIL"
    if [ $TOTAL -gt 0 ]; then
        RATE=$(echo "scale=1; $SUCCESS * 100 / $TOTAL" | bc)
        echo "Success rate:   ${RATE}%"
    fi
    if [ "$BASELINE_SET" = true ]; then
        echo "Baseline nodes: $BASELINE_COUNT ($BASELINE_NODES)"
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

# Build node_cmd.sh args
CMD_ARGS=(-d -g "$GATEWAY_HOST" -p "$GATEWAY_PORT")
if [ -n "$RETRIES" ]; then
    CMD_ARGS+=(-a "$RETRIES")
fi

echo "Discovery test: interval=${INTERVAL}s, expected=${EXPECTED_COUNT:-any}"
echo "Press Ctrl+C to stop and see results"
echo "----------------------------------------"

ITERATION=0
while true; do
    ITERATION=$((ITERATION + 1))
    TOTAL=$((TOTAL + 1))

    RESPONSE=$("$SCRIPT_DIR/node_cmd.sh" "${CMD_ARGS[@]}" 2>&1)
    CMD_EXIT=$?

    TIMESTAMP=$(date '+%H:%M:%S')

    if [ $CMD_EXIT -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "[$TIMESTAMP] #$ITERATION: FAIL - $RESPONSE"
    else
        # Parse response: sorted comma-separated node list and count
        NODES=$(echo "$RESPONSE" | jq -r '.nodes | sort | join(", ")' 2>/dev/null)
        NODE_COUNT=$(echo "$RESPONSE" | jq '.count' 2>/dev/null)

        if [ -z "$NODE_COUNT" ] || [ "$NODE_COUNT" = "null" ]; then
            FAIL=$((FAIL + 1))
            echo "[$TIMESTAMP] #$ITERATION: FAIL - bad response: $RESPONSE"
        elif [ "$BASELINE_SET" = false ]; then
            # First successful round — establish baseline
            BASELINE_NODES="$NODES"
            BASELINE_COUNT="$NODE_COUNT"
            BASELINE_SET=true

            if [ "$EXPECTED_COUNT" -gt 0 ] && [ "$NODE_COUNT" -ne "$EXPECTED_COUNT" ]; then
                echo "[$TIMESTAMP] #$ITERATION: ERROR - expected $EXPECTED_COUNT nodes, found $NODE_COUNT: $NODES"
                echo ""
                echo "Aborting: node count does not match expected."
                exit 1
            fi

            SUCCESS=$((SUCCESS + 1))
            echo "[$TIMESTAMP] #$ITERATION: BASELINE - $NODE_COUNT nodes: $NODES"
        elif [ "$NODES" = "$BASELINE_NODES" ]; then
            SUCCESS=$((SUCCESS + 1))
            echo "[$TIMESTAMP] #$ITERATION: OK ($NODE_COUNT nodes)"
        else
            MISMATCH=$((MISMATCH + 1))
            # Show what changed
            MISSING=""
            EXTRA=""
            for node in $(echo "$BASELINE_NODES" | tr ', ' '\n' | grep -v '^$'); do
                if ! echo "$NODES" | grep -qw "$node"; then
                    MISSING="$MISSING $node"
                fi
            done
            for node in $(echo "$NODES" | tr ', ' '\n' | grep -v '^$'); do
                if ! echo "$BASELINE_NODES" | grep -qw "$node"; then
                    EXTRA="$EXTRA $node"
                fi
            done
            MSG="[$TIMESTAMP] #$ITERATION: MISMATCH - got $NODE_COUNT nodes: $NODES"
            [ -n "$MISSING" ] && MSG="$MSG (missing:$MISSING)"
            [ -n "$EXTRA" ] && MSG="$MSG (extra:$EXTRA)"
            echo "$MSG"
        fi
    fi

    # Check if we've reached the round limit
    if [ "$ROUNDS" -gt 0 ] && [ "$ITERATION" -ge "$ROUNDS" ]; then
        cleanup
    fi

    sleep "$INTERVAL"
done
