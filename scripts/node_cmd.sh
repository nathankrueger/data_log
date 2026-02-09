#!/bin/bash
#
# Generic command sender for LoRa nodes via gateway HTTP API.
#
# Usage: node_cmd.sh -n <node_id> -c <command> [-a <arg>]... [-w] [-g <gateway>] [-p <port>]
#
# Options:
#   -n <node_id>  Target node ID (required for -w, optional for broadcast)
#   -c <command>  Command name (required)
#   -a <arg>      Command argument (can be repeated)
#   -w            Wait for response (uses GET endpoint with 10s timeout)
#   -g <gateway>  Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>     Gateway port (default: $GATEWAY_PORT or 5001)
#   -h            Show this help
#
# Examples:
#   node_cmd.sh -n patio -c ping                    # Ping specific node (fire-and-forget)
#   node_cmd.sh -c ping                             # Broadcast ping to all nodes
#   node_cmd.sh -n patio -c echo -a "hello" -w      # Echo with response wait
#   node_cmd.sh -n patio -c set_interval -a 30      # Set broadcast interval

usage() {
    head -n 18 "$0" | tail -n 17 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

NODE_ID=""
COMMAND=""
ARGS=()
WAIT=false
OPT_HOST=""
OPT_PORT=""

while getopts "n:c:a:wg:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        c)
            COMMAND="$OPTARG"
            ;;
        a)
            ARGS+=("$OPTARG")
            ;;
        w)
            WAIT=true
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

# Command is required
if [ -z "$COMMAND" ]; then
    echo "Error: -c <command> is required"
    usage
fi

# Wait mode requires node_id
if [ "$WAIT" = true ] && [ -z "$NODE_ID" ]; then
    echo "Error: -w requires -n <node_id>"
    usage
fi

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

if [ "$WAIT" = true ]; then
    # GET endpoint: /{cmd}/{node_id}?a=arg1&a=arg2
    URL="http://$GATEWAY_HOST:$GATEWAY_PORT/$COMMAND/$NODE_ID"

    # Build query string for args
    QUERY=""
    for arg in "${ARGS[@]}"; do
        if [ -z "$QUERY" ]; then
            QUERY="?a=$(printf '%s' "$arg" | jq -sRr @uri)"
        else
            QUERY="$QUERY&a=$(printf '%s' "$arg" | jq -sRr @uri)"
        fi
    done

    curl -s -w "\n%{http_code}" "$URL$QUERY"
else
    # POST endpoint: /command with JSON body
    # Build args JSON array
    ARGS_JSON=$(printf '%s\n' "${ARGS[@]}" | jq -R . | jq -s .)

    JSON_BODY=$(jq -n \
        --arg cmd "$COMMAND" \
        --argjson args "$ARGS_JSON" \
        --arg node_id "$NODE_ID" \
        '{cmd: $cmd, args: $args, node_id: $node_id}')

    curl -s -X POST "http://$GATEWAY_HOST:$GATEWAY_PORT/command" \
        -H "Content-Type: application/json" \
        -d "$JSON_BODY"
fi
