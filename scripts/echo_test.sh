#!/bin/bash
#
# Echo command reliability test.
#
# Runs the echo command in a loop at a configurable rate and collects
# success/failure statistics.
#
# Requires: 'jq' (sudo apt install jq)
#
# Usage: echo_test.sh [-n <node_ids>] [-i <interval>] [-c <count>] [-b] [-g <gateway>] [-p <port>]
#
# Options:
#   -n <node_ids>  Comma-separated node IDs, or omit to auto-discover
#   -i <interval>  Seconds between echo commands (default: 1)
#   -c <count>     Number of iterations (default: infinite, Ctrl+C to stop)
#   -b             Broadcast mode: send to all nodes at once, expect all ACKs
#   -g <gateway>   Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>      Gateway port (default: $GATEWAY_PORT or 5001)
#   -h             Show this help
#
# Each echo sends a unique millisecond timestamp and validates the response.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    head -n 19 "$0" | tail -n 18 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

NODE_ID=""
INTERVAL=1
COUNT=0  # 0 = infinite
BROADCAST=false
RATE_PRECISION=3  # decimal places for success rate
OPT_HOST=""
OPT_PORT=""

while getopts "n:i:c:bg:p:h" opt; do
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
        b)
            BROADCAST=true
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

# Auto-discover nodes if not specified
if [ -z "$NODE_ID" ]; then
    echo "Running discovery (3 passes)..."
    DISCOVER_OUT=$("$SCRIPT_DIR/discover_test.sh" -q -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
    DISCOVER_EXIT=$?
    if [ $DISCOVER_EXIT -ne 0 ]; then
        echo "Error: Discovery failed - $DISCOVER_OUT"
        exit 1
    fi
    NODE_ID="$DISCOVER_OUT"
    echo "Discovered nodes: $NODE_ID"
fi

# Parse comma-separated nodes into array
IFS=',' read -ra NODES <<< "$NODE_ID"
NUM_NODES=${#NODES[@]}

if [ $NUM_NODES -eq 0 ]; then
    echo "Error: no nodes found"
    exit 1
fi

# Calculate inter-node delay (evenly distributed across interval)
NODE_DELAY=$(echo "scale=$RATE_PRECISION; $INTERVAL / $NUM_NODES" | bc)

# Statistics (combined)
TOTAL=0
SUCCESS=0
FAIL=0
MISMATCH=0

# Per-node statistics
declare -A NODE_TOTAL NODE_SUCCESS NODE_FAIL NODE_MISMATCH
for node in "${NODES[@]}"; do
    NODE_TOTAL[$node]=0
    NODE_SUCCESS[$node]=0
    NODE_FAIL[$node]=0
    NODE_MISMATCH[$node]=0
done

# Cleanup handler
cleanup() {
    echo ""
    echo "========================================"
    echo "Echo Test Results"
    echo "========================================"

    # Per-node breakdown (only if multiple nodes)
    if [ $NUM_NODES -gt 1 ]; then
        for node in "${NODES[@]}"; do
            local n_total=${NODE_TOTAL[$node]}
            local n_success=${NODE_SUCCESS[$node]}
            local n_mismatch=${NODE_MISMATCH[$node]}
            local n_fail=${NODE_FAIL[$node]}
            if [ $n_total -gt 0 ]; then
                local n_rate=$(echo "scale=$RATE_PRECISION; $n_success * 100 / $n_total" | bc)
                printf "%-12s %d/%d (%s%%) [mismatch=%d, fail=%d]\n" \
                    "$node:" "$n_success" "$n_total" "$n_rate" "$n_mismatch" "$n_fail"
            else
                printf "%-12s no data\n" "$node:"
            fi
        done
        echo "----------------------------------------"
    fi

    # Combined totals
    echo "Total attempts: $TOTAL"
    echo "Successful:     $SUCCESS"
    echo "Mismatched:     $MISMATCH"
    echo "Failed:         $FAIL"
    if [ $TOTAL -gt 0 ]; then
        RATE=$(echo "scale=$RATE_PRECISION; $SUCCESS * 100 / $TOTAL" | bc)
        echo "Success rate:   ${RATE}%"
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

if [ "$BROADCAST" = true ]; then
    echo "Echo test: BROADCAST to $NUM_NODES nodes, interval=${INTERVAL}s"
else
    if [ $NUM_NODES -eq 1 ]; then
        echo "Echo test: node=${NODES[0]}, interval=${INTERVAL}s"
    else
        echo "Echo test: nodes=${NODE_ID}, interval=${INTERVAL}s (${NODE_DELAY}s between nodes)"
    fi
fi
echo "Press Ctrl+C to stop and see results"
echo "----------------------------------------"

ITERATION=0
NODE_IDX=0

if [ "$BROADCAST" = true ]; then
    # Broadcast mode: send to all nodes at once
    while true; do
        ITERATION=$((ITERATION + 1))

        # Generate unique data: millisecond timestamp
        SEND_DATA=$(date '+%s%3N')

        # Run broadcast echo command with expected_acks = number of nodes
        RESPONSE=$("$SCRIPT_DIR/node_cmd.sh" -c echo -a "$SEND_DATA" -e "$NUM_NODES" -w -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
        CMD_EXIT=$?

        TIMESTAMP=$(date '+%H:%M:%S')

        if [ $CMD_EXIT -eq 0 ]; then
            # Parse broadcast response - contains acked_nodes and responses
            ACKED_NODES=$(echo "$RESPONSE" | jq -r '.acked_nodes // [] | join(",")' 2>/dev/null)
            ACKED_COUNT=$(echo "$RESPONSE" | jq -r '.acked_nodes // [] | length' 2>/dev/null)

            # Update per-node stats based on who responded
            for node in "${NODES[@]}"; do
                NODE_TOTAL[$node]=$((NODE_TOTAL[$node] + 1))
                TOTAL=$((TOTAL + 1))

                # Check if this node is in acked_nodes
                if echo "$RESPONSE" | jq -e ".acked_nodes | index(\"$node\")" > /dev/null 2>&1; then
                    # Check if response matches
                    ECHOED_DATA=$(echo "$RESPONSE" | jq -r ".responses[\"$node\"].r // .responses[\"$node\"].data // \"\"" 2>/dev/null)
                    if [ "$ECHOED_DATA" = "$SEND_DATA" ]; then
                        SUCCESS=$((SUCCESS + 1))
                        NODE_SUCCESS[$node]=$((NODE_SUCCESS[$node] + 1))
                    else
                        MISMATCH=$((MISMATCH + 1))
                        NODE_MISMATCH[$node]=$((NODE_MISMATCH[$node] + 1))
                    fi
                else
                    FAIL=$((FAIL + 1))
                    NODE_FAIL[$node]=$((NODE_FAIL[$node] + 1))
                fi
            done

            echo "[$TIMESTAMP] BROADCAST #$ITERATION: $ACKED_COUNT/$NUM_NODES ACKs (sent=$SEND_DATA, nodes=$ACKED_NODES)"
        else
            # Complete failure - no response at all
            for node in "${NODES[@]}"; do
                NODE_TOTAL[$node]=$((NODE_TOTAL[$node] + 1))
                NODE_FAIL[$node]=$((NODE_FAIL[$node] + 1))
                TOTAL=$((TOTAL + 1))
                FAIL=$((FAIL + 1))
            done
            echo "[$TIMESTAMP] BROADCAST #$ITERATION: FAIL - $RESPONSE"
        fi

        # Check if we've reached the count limit
        if [ $COUNT -gt 0 ] && [ $ITERATION -ge $COUNT ]; then
            cleanup
        fi

        sleep "$INTERVAL"
    done
else
    # Unicast mode: iterate through nodes
    while true; do
        CURRENT_NODE="${NODES[$NODE_IDX]}"
        ITERATION=$((ITERATION + 1))

        # Generate unique data: millisecond timestamp
        SEND_DATA=$(date '+%s%3N')

        # Run echo command - node_cmd.sh -w prints JSON body on success (exit 0),
        # or prints error to stderr and exits non-zero on failure
        RESPONSE=$("$SCRIPT_DIR/node_cmd.sh" -n "$CURRENT_NODE" -c echo -a "$SEND_DATA" -w -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
        CMD_EXIT=$?

        # Increment totals only after command completes (so Ctrl+C mid-test doesn't inflate count)
        TOTAL=$((TOTAL + 1))
        NODE_TOTAL[$CURRENT_NODE]=$((NODE_TOTAL[$CURRENT_NODE] + 1))

        TIMESTAMP=$(date '+%H:%M:%S')

        if [ $CMD_EXIT -eq 0 ]; then
            # Extract echoed data from response JSON
            ECHOED_DATA=$(echo "$RESPONSE" | jq -r '.r // .data // .echo // .payload' 2>/dev/null)

            if [ "$ECHOED_DATA" = "$SEND_DATA" ]; then
                SUCCESS=$((SUCCESS + 1))
                NODE_SUCCESS[$CURRENT_NODE]=$((NODE_SUCCESS[$CURRENT_NODE] + 1))
                echo "[$TIMESTAMP] $CURRENT_NODE #$ITERATION: OK (sent=$SEND_DATA)"
            else
                MISMATCH=$((MISMATCH + 1))
                NODE_MISMATCH[$CURRENT_NODE]=$((NODE_MISMATCH[$CURRENT_NODE] + 1))
                echo "[$TIMESTAMP] $CURRENT_NODE #$ITERATION: MISMATCH (sent=$SEND_DATA, got=$ECHOED_DATA)"
            fi
        else
            FAIL=$((FAIL + 1))
            NODE_FAIL[$CURRENT_NODE]=$((NODE_FAIL[$CURRENT_NODE] + 1))
            echo "[$TIMESTAMP] $CURRENT_NODE #$ITERATION: FAIL - $RESPONSE"
        fi

        # Check if we've reached the count limit
        if [ $COUNT -gt 0 ] && [ $ITERATION -ge $COUNT ]; then
            cleanup
        fi

        # Move to next node
        NODE_IDX=$(( (NODE_IDX + 1) % NUM_NODES ))

        sleep "$NODE_DELAY"
    done
fi
