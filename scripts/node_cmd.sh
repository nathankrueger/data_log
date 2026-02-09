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

    # Build query string for args - split comma-separated values
    QUERY=""
    if [ ${#ARGS[@]} -gt 0 ]; then
        ALL_ARGS=$(IFS=,; echo "${ARGS[*]}")
        IFS=',' read -ra ARG_ARRAY <<< "$ALL_ARGS"
        for arg in "${ARG_ARRAY[@]}"; do
            if [ -z "$QUERY" ]; then
                QUERY="?a=$(printf '%s' "$arg" | jq -sRr @uri)"
            else
                QUERY="$QUERY&a=$(printf '%s' "$arg" | jq -sRr @uri)"
            fi
        done
    fi

    # Capture stderr separately to get clean error messages
    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS --max-time 10 -w "\n%{http_code}" "$URL$QUERY" 2>"$STDERR_FILE")
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    # Output response (includes HTTP code on last line for caller to parse)
    echo "$RESPONSE"
else
    # POST endpoint: /command with JSON body
    # Build args JSON array - split comma-separated values
    if [ ${#ARGS[@]} -gt 0 ]; then
        # Join all -a args with commas, then split by comma
        ALL_ARGS=$(IFS=,; echo "${ARGS[*]}")
        IFS=',' read -ra ARG_ARRAY <<< "$ALL_ARGS"
        ARGS_JSON=$(printf '%s\n' "${ARG_ARRAY[@]}" | jq -R . | jq -s .)
    else
        ARGS_JSON="[]"
    fi

    JSON_BODY=$(jq -n \
        --arg cmd "$COMMAND" \
        --argjson args "$ARGS_JSON" \
        --arg node_id "$NODE_ID" \
        '{cmd: $cmd, args: $args, node_id: $node_id}')

    # Capture stderr separately to get clean error messages
    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS -X POST "http://$GATEWAY_HOST:$GATEWAY_PORT/command" \
        -H "Content-Type: application/json" \
        -d "$JSON_BODY" \
        --max-time 5 \
        -w "\n%{http_code}" 2>"$STDERR_FILE")
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    # Parse response body and HTTP code
    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "$BODY"
    else
        echo "Error: HTTP $HTTP_CODE - $BODY" >&2
        exit 1
    fi
fi
