#!/bin/bash
#
# Echo command reliability test.
# Each echo sends a unique millisecond timestamp and validates the response.
# Requires: 'jq' (sudo apt install jq)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: echo_test.sh [-n <node_ids>] [-e <expected>] [-i <interval>] [-c <count>] [-b]
                    [-g <gateway>] [-p <port>]

Options:
  -n <node_ids>  Comma-separated node IDs, or omit to auto-discover
  -e <expected>  Expected number of nodes (speeds up discovery, validates count)
  -i <interval>  Seconds between echo commands (default: 1)
  -c <count>     Number of iterations (default: infinite, Ctrl+C to stop)
  -b             Broadcast mode: send to all nodes at once, expect all ACKs
  -g <gateway>   Gateway host (default: $GATEWAY_HOST or localhost)
  -p <port>      Gateway port (default: $GATEWAY_PORT or 5001)
  -h             Show this help
EOF
    exit 1
}

NODE_ID=""
EXPECTED_NODES=0  # 0 = not specified
INTERVAL=1
COUNT=0  # 0 = infinite
BROADCAST=false
RATE_PRECISION=3  # decimal places for success rate
OPT_HOST=""
OPT_PORT=""

while getopts "n:e:i:c:bg:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        e)
            EXPECTED_NODES="$OPTARG"
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
    if [ "$BROADCAST" = true ] && [ "$EXPECTED_NODES" -gt 0 ]; then
        # Broadcast with -e: skip discovery, build baseline from first response
        echo "Broadcast mode with -e $EXPECTED_NODES: skipping discovery"
        NUM_NODES=$EXPECTED_NODES
        NODES=()  # Empty array - will be populated from first response
    elif [ "$EXPECTED_NODES" -gt 0 ]; then
        # Unicast with -e: single discovery pass with count validation
        echo "Running discovery (1 pass, expecting $EXPECTED_NODES nodes)..."
        DISCOVER_OUT=$("$SCRIPT_DIR/node_cmd.sh" -d -g "$GATEWAY_HOST" -p "$GATEWAY_PORT" 2>&1)
        DISCOVER_EXIT=$?
        if [ $DISCOVER_EXIT -ne 0 ]; then
            echo "Error: Discovery failed - $DISCOVER_OUT"
            exit 1
        fi
        # Parse response
        NODE_ID=$(echo "$DISCOVER_OUT" | jq -r '.nodes | sort | join(",")' 2>/dev/null)
        NODE_COUNT=$(echo "$DISCOVER_OUT" | jq '.count' 2>/dev/null)
        if [ -z "$NODE_COUNT" ] || [ "$NODE_COUNT" = "null" ]; then
            echo "Error: Failed to parse discovery response - $DISCOVER_OUT"
            exit 1
        fi
        if [ "$NODE_COUNT" -ne "$EXPECTED_NODES" ]; then
            echo "Error: Expected $EXPECTED_NODES nodes, found $NODE_COUNT: $NODE_ID"
            exit 1
        fi
        echo "Discovered nodes: $NODE_ID"
    else
        # Original behavior: 3-pass discovery
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
fi

# Parse comma-separated nodes into array (may be empty for broadcast with -e)
if [ -n "$NODE_ID" ]; then
    IFS=',' read -ra NODES <<< "$NODE_ID"
    NUM_NODES=${#NODES[@]}
fi

# For non-broadcast mode, we need nodes
if [ "$BROADCAST" != true ] && [ ${#NODES[@]} -eq 0 ]; then
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

# Per-node statistics (initialized for known nodes, may be extended dynamically for broadcast -e)
declare -A NODE_TOTAL NODE_SUCCESS NODE_FAIL NODE_MISMATCH
for node in "${NODES[@]}"; do
    NODE_TOTAL[$node]=0
    NODE_SUCCESS[$node]=0
    NODE_FAIL[$node]=0
    NODE_MISMATCH[$node]=0
done

# For broadcast with -e, track whether baseline has been established
BASELINE_SET=false

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
            ACKED_NODES_STR=$(echo "$RESPONSE" | jq -r '.acked_nodes // [] | sort | join(",")' 2>/dev/null)
            ACKED_COUNT=$(echo "$RESPONSE" | jq -r '.acked_nodes // [] | length' 2>/dev/null)

            # If baseline not yet established (broadcast with -e, first response)
            if [ "$BASELINE_SET" = false ] && [ ${#NODES[@]} -eq 0 ]; then
                # Build baseline from first response (only if we got nodes)
                if [ -n "$ACKED_NODES_STR" ]; then
                    IFS=',' read -ra NODES <<< "$ACKED_NODES_STR"
                    for node in "${NODES[@]}"; do
                        NODE_TOTAL[$node]=0
                        NODE_SUCCESS[$node]=0
                        NODE_FAIL[$node]=0
                        NODE_MISMATCH[$node]=0
                    done
                    BASELINE_SET=true
                    BASELINE_NODES_STR="$ACKED_NODES_STR"
                    echo "Baseline established: ${#NODES[@]} nodes ($ACKED_NODES_STR)"
                else
                    echo "[$TIMESTAMP] BROADCAST #$ITERATION: No nodes responded, waiting for baseline..."
                fi
            fi

            # Determine if this iteration is a success (same set as baseline)
            ITERATION_SUCCESS=true
            if [ "$BASELINE_SET" = true ]; then
                # For baseline comparison: check if acked set matches baseline exactly
                if [ "$ACKED_NODES_STR" != "$BASELINE_NODES_STR" ]; then
                    ITERATION_SUCCESS=false
                fi
            fi

            # Update per-node stats based on who responded (baseline nodes only)
            for node in "${NODES[@]}"; do
                NODE_TOTAL[$node]=$((NODE_TOTAL[$node] + 1))
                TOTAL=$((TOTAL + 1))

                # Check if this node is in acked_nodes
                if echo "$RESPONSE" | jq -e ".acked_nodes | index(\"$node\")" > /dev/null 2>&1; then
                    # Check if response matches - try multiple possible field names
                    NODE_RESP=$(echo "$RESPONSE" | jq -r ".responses[\"$node\"]" 2>/dev/null)
                    ECHOED_DATA=$(echo "$NODE_RESP" | jq -r '.r // .data // .echo // ""' 2>/dev/null)
                    if [ "$ECHOED_DATA" = "$SEND_DATA" ]; then
                        SUCCESS=$((SUCCESS + 1))
                        NODE_SUCCESS[$node]=$((NODE_SUCCESS[$node] + 1))
                    else
                        MISMATCH=$((MISMATCH + 1))
                        NODE_MISMATCH[$node]=$((NODE_MISMATCH[$node] + 1))
                        # Debug: show what we got
                        echo "  [DEBUG] $node: expected='$SEND_DATA' got='$ECHOED_DATA' raw='$NODE_RESP'" >&2
                    fi
                else
                    FAIL=$((FAIL + 1))
                    NODE_FAIL[$node]=$((NODE_FAIL[$node] + 1))
                fi
            done

            # Report iteration result
            if [ "$BASELINE_SET" = true ]; then
                # Broadcast with -e: show OK/FAIL based on baseline comparison
                if [ "$ITERATION_SUCCESS" = true ]; then
                    echo "[$TIMESTAMP] BROADCAST #$ITERATION: OK $ACKED_COUNT/$NUM_NODES ACKs (sent=$SEND_DATA, nodes=$ACKED_NODES_STR)"
                else
                    echo "[$TIMESTAMP] BROADCAST #$ITERATION: FAIL $ACKED_COUNT/$NUM_NODES ACKs (sent=$SEND_DATA, nodes=$ACKED_NODES_STR)"
                fi
            else
                # Original behavior: just show count
                echo "[$TIMESTAMP] BROADCAST #$ITERATION: $ACKED_COUNT/$NUM_NODES ACKs (sent=$SEND_DATA, nodes=$ACKED_NODES_STR)"
            fi
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
